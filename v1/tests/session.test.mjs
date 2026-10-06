import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function sessionModule({ authenticated = true, selectedAccount = "one", membership = "active", expiresIn = 3600 } = {}) {
  const exchanges = []
  const me = { user: { id: "operator" }, memberships: [{ account_id: "one", status: membership }] }
  const code = ts.transpileModule(readFileSync(new URL("../lib/fabric/session.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  const dependencies = {
    "server-only": {},
    react: { cache: (fn) => fn },
    "next/headers": { cookies: async () => ({ get: () => selectedAccount ? { value: selectedAccount } : undefined }) },
    "next/navigation": { redirect: (path) => { throw new Error(`redirect:${path}`) } },
    "@/lib/auth0": { isAuth0Configured: true, auth0: { getSession: async () => authenticated ? { user: { sub: "auth0|operator" } } : null, getAccessToken: async () => ({ token: "assertion-for-test" }) } },
    "@/lib/fabric/client": { async controlPlaneRequest(path, options) {
      if (path === "/v1/token") {
        const body = JSON.parse(options.body)
        exchanges.push(body)
        return { access_token: `${body.account_id}:${body.audience}:token-${exchanges.length}`, expires_in: expiresIn }
      }
      if (path === "/v1/me") return me
      if (path === "/v1/accounts/one") return { id: "one" }
      if (path === "/v1/self") return { account_id: "one", scopes: ["deployments:read"] }
      throw new Error(`Unexpected path ${path}`)
    } },
  }
  vm.runInNewContext(code, { exports, process: { env: {} }, require: (name) => {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency ${name}`)
    return dependencies[name]
  } })
  return { session: exports, exchanges }
}

test("control and inference tokens use separate account- and subject-scoped cache entries", async () => {
  const { session, exchanges } = sessionModule()
  const control = await session.getFabricAccessToken("one")
  const inference = await session.getFabricInferenceToken("one")
  assert.notEqual(control, inference)
  assert.equal(await session.getFabricAccessToken("one"), control)
  assert.equal(await session.getFabricInferenceToken("one"), inference)
  assert.notEqual(await session.getFabricInferenceToken("two"), inference)
  assert.deepEqual(exchanges.map(({ account_id, audience }) => ({ account_id, audience })), [{ account_id: "one", audience: "fabric-control" }, { account_id: "one", audience: "fabric-inference" }, { account_id: "two", audience: "fabric-inference" }])
  assert.ok(exchanges.every((item) => item.grant_type === "auth0_token" && item.assertion === "assertion-for-test"))
})

test("expired Fabric tokens are exchanged again before the next request", async () => {
  const { session, exchanges } = sessionModule({ expiresIn: 1 })
  assert.notEqual(await session.getFabricInferenceToken("one"), await session.getFabricInferenceToken("one"))
  assert.equal(exchanges.length, 2)
})

test("unauthenticated sessions cannot obtain a Fabric token or account context", async () => {
  const { session, exchanges } = sessionModule({ authenticated: false })
  await assert.rejects(session.getFabricInferenceToken("one"), /redirect:\/auth\/login/)
  await assert.rejects(session.getConsoleContext(), /redirect:\/auth\/login/)
  assert.equal(exchanges.length, 0)
})

test("account selection requires an active membership before loading context", async () => {
  for (const options of [{ selectedAccount: "two" }, { membership: "revoked" }, { selectedAccount: null }]) {
    const { session, exchanges } = sessionModule(options)
    await assert.rejects(session.getConsoleContext(), /redirect:\/onboarding/)
    assert.equal(exchanges.length, 0)
  }
  const { session } = sessionModule()
  assert.equal((await session.getConsoleContext()).account.id, "one")
})
