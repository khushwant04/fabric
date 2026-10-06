import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative, dependencies = {}) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, require(name) { if (Object.hasOwn(dependencies, name)) return dependencies[name]; throw new Error(`Unexpected dependency: ${name}`) } })
  return exports
}
const { confirmsModelDeletion } = compile("../lib/fabric/resource-state.ts")

class FabricApiError extends Error {
  constructor(status, message) { super(message); this.status = status }
}

function fixture({ accountId = "account-one", write = true, deploymentAccount = accountId, modelAlias = "model-a", failure } = {}) {
  const calls = []
  const revalidated = []
  const redirected = []
  const actions = compile("../lib/fabric/deployment-actions.ts", {
    "next/cache": { revalidatePath: (path) => revalidated.push(path) },
    "next/navigation": { unstable_rethrow() {}, redirect: (path) => redirected.push(path) },
    "@/lib/fabric/resource-state": { confirmsModelDeletion },
    "@/lib/fabric/client": { FabricApiError, async controlPlaneRequest(path, init) {
      calls.push({ path, method: init.method ?? "GET", token: init.token })
      if (failure) throw failure
      return { id: "dep-one", account_id: deploymentAccount, model_alias: modelAlias }
    } },
    "@/lib/fabric/session": {
      getConsoleContext: async () => ({ account: { id: accountId } }),
      requireScope: (_context, scope) => { assert.equal(scope, "deployments:write"); if (!write) throw new Error("Denied") },
      getFabricAccessToken: async (account) => `${account}-token`,
    },
  })
  async function run(confirmation, id = "dep-one") { const form = new FormData(); if (confirmation !== undefined) form.set("confirmation", confirmation); return actions.confirmDeleteDeployment(id, {}, form) }
  return { run, calls, revalidated, redirected }
}

test("confirmed deletion re-reads selected-account resource and uses its token", async () => {
  const item = fixture()
  await item.run("model-a")
  assert.deepEqual(item.calls, [ { path: "/v1/accounts/account-one/deployments/dep-one", method: "GET", token: "account-one-token" }, { path: "/v1/accounts/account-one/deployments/dep-one", method: "DELETE", token: "account-one-token" } ])
  assert.deepEqual(item.revalidated, ["/deployments", "/dashboard"])
  assert.deepEqual(item.redirected, ["/deployments"])
})

for (const confirmation of [undefined, "", "Model-a", "model-a "]) {
  test(`missing or mismatched confirmation (${JSON.stringify(confirmation)}) never deletes`, async () => {
    const item = fixture()
    assert.match((await item.run(confirmation)).error, /exactly/)
    assert.equal(item.calls.length, 1)
    assert.equal(item.redirected.length, 0)
  })
}

test("renamed aliases and switched or foreign accounts cannot reuse old consent", async () => {
  const renamed = fixture({ modelAlias: "model-new" })
  assert.match((await renamed.run("model-a")).error, /model-new/)
  assert.equal(renamed.calls.length, 1)
  const foreign = fixture({ accountId: "account-two", deploymentAccount: "account-one" })
  assert.match((await foreign.run("model-a")).error, /selected account/)
  assert.equal(foreign.calls.length, 1)
  assert.equal(foreign.calls[0].token, "account-two-token")
})

test("read-only scopes and invalid ids prevent any API mutation", async () => {
  const denied = fixture({ write: false })
  assert.ok((await denied.run("model-a")).error)
  assert.equal(denied.calls.length, 0)
  const item = fixture()
  assert.ok((await item.run("model-a", "../other")).error)
  assert.equal(item.calls.length, 0)
})

test("failed deletion returns safe error without redirecting or claiming cleanup", async () => {
  const item = fixture({ failure: new Error("private control-plane details") })
  assert.equal((await item.run("model-a")).error, "Deletion could not be completed. Try again.")
  assert.equal(item.redirected.length, 0)
  assert.equal(item.revalidated.length, 0)
})

test("API errors keep backend details private and preserve authorization failures", async () => {
  for (const status of [403, 404, 500, 502]) {
    const item = fixture({ failure: new FabricApiError(status, "private control-plane details") })
    const result = await item.run("model-a")
    assert.doesNotMatch(result.error, /private/)
    assert.match(result.error, [403, 404].includes(status) ? /permission/ : /could not be completed/)
    assert.equal(item.redirected.length, 0)
    assert.equal(item.revalidated.length, 0)
  }
})
