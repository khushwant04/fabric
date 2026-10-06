import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative, dependencies, env = {}) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, process: { env }, require: (name) => {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency ${name}`)
    return dependencies[name]
  } })
  return exports
}

function fixture(singleTenant = false) {
  const calls = []
  const selections = []
  const tenant = compile("../lib/fabric/single-tenant.ts", { "server-only": {} }, singleTenant ? { FABRIC_SINGLE_TENANT_ACCOUNT_ID: "4c0a0d13-70e5-4a98-9a61-0a278fdc831c" } : {})
  const actions = compile("../app/onboarding/actions.ts", {
    "next/navigation": { redirect: (path) => { throw new Error(`redirect:${path}`) } },
    "@/lib/fabric/single-tenant": tenant,
    "@/lib/fabric/client": { controlPlaneRequest: async (path, init) => { calls.push({ path, init }); return { id: "created-account" } } },
    "@/lib/fabric/session": {
      getMe: async () => ({ memberships: [{ account_id: "active-account", status: "active" }, { account_id: "revoked-account", status: "revoked" }] }),
      getAuth0AccessToken: async () => "assertion",
      setSelectedAccount: async (account) => { selections.push(account) },
    },
  })
  return { actions, calls, selections }
}

test("single-tenant server actions reject account selection and creation before any upstream call", async () => {
  const { actions, calls, selections } = fixture(true)
  const account = new FormData()
  account.set("accountId", "active-account")
  const create = new FormData()
  create.set("name", "New workspace")
  create.set("slug", "new-workspace")
  await assert.rejects(actions.selectAccount(account), /configured by its administrator/)
  await assert.rejects(actions.createAccount(create), /configured by its administrator/)
  assert.equal(calls.length, 0)
  assert.equal(selections.length, 0)
})

test("managed account selection keeps active membership enforcement", async () => {
  const { actions, selections } = fixture()
  for (const id of ["revoked-account", "foreign-account"]) {
    const form = new FormData()
    form.set("accountId", id)
    await assert.rejects(actions.selectAccount(form), /do not have access/)
  }
  const form = new FormData()
  form.set("accountId", "active-account")
  await assert.rejects(actions.selectAccount(form), /redirect:\/dashboard/)
  assert.deepEqual(selections, ["active-account"])
})

test("managed account creation keeps its existing authenticated flow", async () => {
  const { actions, calls, selections } = fixture()
  const form = new FormData()
  form.set("name", "New workspace")
  form.set("slug", "new-workspace")
  await assert.rejects(actions.createAccount(form), /redirect:\/dashboard/)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, "/v1/accounts")
  assert.equal(calls[0].init.token, "assertion")
  assert.deepEqual(selections, ["created-account"])
})
