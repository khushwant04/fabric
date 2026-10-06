import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function fixture({ failure, controlFlow = false } = {}) {
  let authenticated = false
  let refreshes = 0
  const code = ts.transpileModule(readFileSync(new URL("../lib/fabric/refresh-actions.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, require(name) {
    if (name === "next/cache") return { refresh() { assert.equal(authenticated, true); refreshes++ } }
    if (name === "next/navigation") return { unstable_rethrow(error) { if (controlFlow) throw error } }
    if (name === "@/lib/fabric/session") return { async getConsoleContext() {
      if (failure) throw failure
      authenticated = true
      return { account: { id: "selected-account" }, identity: { scopes: ["deployments:read"] } }
    } }
    throw new Error(`Unexpected dependency: ${name}`)
  } })
  return { run: exports.refreshConsoleView, refreshes: () => refreshes }
}

test("refresh revalidates current server-selected account membership before refetching its RSC tree", async () => {
  const item = fixture()
  assert.equal((await item.run()).ok, true)
  assert.equal(item.refreshes(), 1)
})

test("authentication and membership errors do not refresh or leak backend details", async () => {
  const item = fixture({ failure: new Error("private membership or control-plane detail") })
  const result = await item.run()
  assert.equal(result.error, "Refresh failed. Check your connection and try again.")
  assert.equal(item.refreshes(), 0)
})

test("Next authentication redirects preserve framework control flow", async () => {
  const failure = new Error("NEXT_REDIRECT")
  const item = fixture({ failure, controlFlow: true })
  await assert.rejects(item.run(), (error) => error === failure)
  assert.equal(item.refreshes(), 0)
})
