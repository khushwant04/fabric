import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative, dependencies = {}) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, Error, URL, require(name) { if (Object.hasOwn(dependencies, name)) return dependencies[name]; throw new Error(`Unexpected dependency: ${name}`) } })
  return exports
}

const plain = (value) => JSON.parse(JSON.stringify(value))
const identityProvider = compile("../lib/fabric/identity-provider.ts")
const { parseIdentityProvider } = identityProvider

function form(values = {}) {
  const result = new FormData()
  for (const [key, value] of Object.entries({ issuer: "https://login.example.com/", audience: "fabric-console", ...values })) result.set(key, value)
  return result
}

test("the provider form maps to the control plane's request with its defaults", () => {
  assert.deepEqual(plain(parseIdentityProvider(form())), {
    issuer: "https://login.example.com",
    audience: "fabric-console",
    jwks_uri: null,
    subject_claim: "sub",
    email_claim: "email",
    auto_provision_role: null,
  })
  const explicit = parseIdentityProvider(form({ jwksUri: "https://login.example.com/keys", subjectClaim: "oid", emailClaim: "upn", autoProvisionRole: "developer" }))
  assert.equal(explicit.jwks_uri, "https://login.example.com/keys")
  assert.equal(explicit.subject_claim, "oid")
  assert.equal(explicit.email_claim, "upn")
  assert.equal(explicit.auto_provision_role, "developer")
})

test("the provider form refuses plain HTTP, credentials in URLs, privileged roles, and odd claims", () => {
  for (const values of [
    { issuer: "http://login.example.com" },
    { issuer: "login.example.com" },
    { issuer: "https://user:secret@login.example.com" },
    { jwksUri: "http://login.example.com/keys" },
    { audience: "" },
    { autoProvisionRole: "owner" },
    { autoProvisionRole: "admin" },
    { subjectClaim: "sub claim" },
    { emailClaim: "email\"; drop" },
  ]) {
    assert.throws(() => parseIdentityProvider(form(values)), undefined, JSON.stringify(values))
  }
})

function actions({ scopes = ["members:read", "members:write"], respond } = {}) {
  const calls = []
  const revalidated = []
  class FabricApiError extends Error { constructor(message, status) { super(message); this.status = status } }
  const module = compile("../lib/fabric/identity-provider-actions.ts", {
    "next/cache": { revalidatePath: (path) => revalidated.push(path) },
    "@/lib/fabric/client": { FabricApiError, async controlPlaneRequest(path, init) { calls.push({ path, init }); return respond ? respond(path, init, FabricApiError) : {} } },
    "@/lib/fabric/identity-provider": identityProvider,
    "@/lib/fabric/session": {
      getConsoleContext: async () => ({ account: { id: "tenant-one" }, identity: { scopes } }),
      getFabricAccessToken: async (accountId) => `${accountId}-token`,
      requireScope(context, scope) { if (!context.identity.scopes.includes(scope)) throw new Error(`Missing required scope: ${scope}`) },
    },
  })
  return { module, calls, revalidated }
}

test("saving replaces the selected account's provider with PUT and refreshes the page", async () => {
  const { module, calls, revalidated } = actions()
  const result = await module.saveIdentityProvider({}, form({ autoProvisionRole: "viewer" }))
  assert.equal(result.ok, true)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, "/v1/accounts/tenant-one/oidc-provider")
  assert.equal(calls[0].init.method, "PUT")
  assert.equal(calls[0].init.token, "tenant-one-token")
  assert.equal(JSON.parse(calls[0].init.body).auto_provision_role, "viewer")
  assert.deepEqual(revalidated, ["/access/identity-provider"])
})

test("members:write is required to save or remove, and nothing is sent without it", async () => {
  const { module, calls } = actions({ scopes: ["members:read"] })
  assert.match((await module.saveIdentityProvider({}, form())).error, /members:write/)
  assert.match((await module.removeIdentityProvider()).error, /members:write/)
  assert.equal(calls.length, 0)
})

test("invalid input and API refusals come back as form errors", async () => {
  const { module, calls } = actions({ respond(_path, _init, FabricApiError) { throw new FabricApiError("This issuer is registered to another account", 403) } })
  assert.match((await module.saveIdentityProvider({}, form({ issuer: "http://insecure.example" }))).error, /https/)
  assert.equal(calls.length, 0)
  assert.equal((await module.saveIdentityProvider({}, form())).error, "This issuer is registered to another account")
})

test("removing deletes the provider, and an already-removed provider counts as removed", async () => {
  const removed = actions()
  assert.equal((await removed.module.removeIdentityProvider()).ok, true)
  assert.equal(removed.calls[0].init.method, "DELETE")
  assert.equal(removed.calls[0].path, "/v1/accounts/tenant-one/oidc-provider")
  const missing = actions({ respond(_path, _init, FabricApiError) { throw new FabricApiError("not found", 404) } })
  assert.equal((await missing.module.removeIdentityProvider()).ok, true)
})
