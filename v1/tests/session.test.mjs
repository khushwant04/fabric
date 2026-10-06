import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

const FIXED_ACCOUNT = "4c0a0d13-70e5-4a98-9a61-0a278fdc831c"

function sessionModule({ authenticated = true, selectedAccount = "one", membership = "active", expiresIn = 3600, singleTenantAccount, identityAccount, identityAudience = "fabric-control" } = {}) {
  const exchanges = []
  const writes = []
  const account = singleTenantAccount || "one"
  const me = { user: { id: "operator" }, memberships: [{ account_id: account, status: membership }] }
  const code = ts.transpileModule(readFileSync(new URL("../lib/fabric/session.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  const dependencies = {
    "server-only": {},
    react: { cache: (fn) => fn },
    "next/headers": { cookies: async () => ({ get: () => selectedAccount ? { value: selectedAccount } : undefined, set: (...args) => writes.push(args) }) },
    "next/navigation": { redirect: (path) => { throw new Error(`redirect:${path}`) } },
    "@/lib/auth0": { isAuth0Configured: true, auth0: { getSession: async () => authenticated ? { user: { sub: "auth0|operator" } } : null, getAccessToken: async () => ({ token: "assertion-for-test" }) } },
    "@/lib/fabric/client": { async controlPlaneRequest(path, options) {
      if (path === "/v1/token") {
        const body = JSON.parse(options.body)
        exchanges.push(body)
        return { access_token: `${body.account_id}:${body.audience}:token-${exchanges.length}`, expires_in: expiresIn }
      }
      if (path === "/v1/me") return me
      if (path === `/v1/accounts/${account}`) return { id: account }
      if (path === "/v1/self") return { account_id: identityAccount || account, audience: identityAudience, scopes: ["deployments:read"] }
      throw new Error(`Unexpected path ${path}`)
    } },
  }
  const process = { env: singleTenantAccount ? { FABRIC_SINGLE_TENANT_ACCOUNT_ID: singleTenantAccount } : {} }
  const tenantExports = {}
  const tenantCode = ts.transpileModule(readFileSync(new URL("../lib/fabric/single-tenant.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  vm.runInNewContext(tenantCode, { exports: tenantExports, process, require: (name) => { assert.equal(name, "server-only"); return {} } })
  dependencies["@/lib/fabric/single-tenant"] = tenantExports
  vm.runInNewContext(code, { exports, process, require: (name) => {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency ${name}`)
    return dependencies[name]
  } })
  return { session: exports, exchanges, writes }
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

test("a fixed workspace ignores foreign and missing browser account cookies", async () => {
  for (const selectedAccount of ["other-account", null]) {
    const { session, exchanges } = sessionModule({ selectedAccount, singleTenantAccount: FIXED_ACCOUNT })
    assert.equal(await session.getSelectedAccountId(), FIXED_ACCOUNT)
    const context = await session.getConsoleContext()
    assert.equal(context.account.id, FIXED_ACCOUNT)
    assert.equal(context.singleTenant, true)
    assert.equal(exchanges.length, 1)
    assert.equal(exchanges[0].account_id, FIXED_ACCOUNT)
  }
})

test("fixed-workspace context and token exchange deny inactive membership", async () => {
  for (const membership of ["revoked", "pending"]) {
    const { session, exchanges } = sessionModule({ singleTenantAccount: FIXED_ACCOUNT, membership })
    await assert.rejects(session.getConsoleContext(), /redirect:\/access-denied/)
    await assert.rejects(session.getFabricInferenceToken(FIXED_ACCOUNT), /redirect:\/access-denied/)
    assert.equal(exchanges.length, 0)
  }
})

test("fixed-workspace tokens cannot target another account and selection cannot write a cookie", async () => {
  const { session, exchanges, writes } = sessionModule({ singleTenantAccount: FIXED_ACCOUNT })
  await assert.rejects(session.getFabricAccessToken("other-account"), /outside the configured workspace/)
  await assert.rejects(session.getFabricInferenceToken("other-account"), /outside the configured workspace/)
  await assert.rejects(session.setSelectedAccount(FIXED_ACCOUNT), /configured by its administrator/)
  assert.equal(exchanges.length, 0)
  assert.equal(writes.length, 0)
})

test("fixed-workspace context rejects upstream account and audience mismatches", async () => {
  for (const options of [{ identityAccount: "other-account" }, { identityAudience: "fabric-inference" }]) {
    const { session } = sessionModule({ singleTenantAccount: FIXED_ACCOUNT, ...options })
    await assert.rejects(session.getConsoleContext(), /identity outside the configured workspace/)
  }
})
