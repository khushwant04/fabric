import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative, dependencies = {}) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, Error, require(name) { if (Object.hasOwn(dependencies, name)) return dependencies[name]; throw new Error(`Unexpected dependency: ${name}`) } })
  return exports
}

const { parseNewDeployment, parsePlacementStamp } = compile("../lib/fabric/new-deployment.ts")
const stampId = "4c0a0d13-70e5-4a98-9a61-0a278fdc831c"
function form(values = {}) {
  const result = new FormData()
  for (const [key, value] of Object.entries({ name: "qwen-4b", modelAlias: "qwen3.5-4b", modelRef: "Qwen/Qwen3.5-4B", replicas: "1", gpuCount: "1", gpuClass: "t4", stampId, ...values })) result.set(key, value)
  return result
}

test("model repository, API alias, and optional serving settings remain distinct", () => {
  const input = parseNewDeployment(form({ maxModelLen: "4096", maxNumSeqs: "8", execution: "eager" }))
  assert.equal(input.modelRef, "Qwen/Qwen3.5-4B")
  assert.equal(input.modelAlias, "qwen3.5-4b")
  assert.equal(input.stampId, stampId)
  assert.equal(input.maxModelLen, 4096)
  assert.equal(input.maxNumSeqs, 8)
  assert.equal(input.execution, "eager")
  const automatic = parseNewDeployment(form({ stampId: "auto", execution: "default" }))
  assert.equal(automatic.stampId, null)
  assert.equal(automatic.maxModelLen, null)
  assert.equal(automatic.execution, null)
})

test("server-side validation rejects URLs, process flags, invalid cluster ids, and unbounded GPU requests", () => {
  for (const values of [{ modelRef: "http://169.254.169.254/" }, { modelRef: "Qwen/Qwen3.5-4B --trust-remote-code" }, { modelRef: "../../private" }, { modelRef: "../private" }, { modelAlias: "auto" }, { stampId: "../../account" }, { gpuCount: "0" }, { gpuCount: "9" }, { replicas: "1.5" }, { maxNumSeqs: "1025" }, { execution: "shell" }]) {
    assert.throws(() => parseNewDeployment(form(values)))
  }
})

test("tool calling keeps model defaults distinct from an explicit disable and parser selection", () => {
  const defaults = parseNewDeployment(form())
  assert.equal(defaults.enableAutoToolChoice, null)
  assert.equal(defaults.toolCallParser, null)
  const disabled = parseNewDeployment(form({ autoToolChoice: "disabled" }))
  assert.equal(disabled.enableAutoToolChoice, false)
  const enabled = parseNewDeployment(form({ autoToolChoice: "enabled", toolCallParser: "qwen3_xml" }))
  assert.equal(enabled.enableAutoToolChoice, true)
  assert.equal(enabled.toolCallParser, "qwen3_xml")
  for (const values of [{ autoToolChoice: "disabled", toolCallParser: "hermes" }, { autoToolChoice: "yes" }, { toolCallParser: "hermes --trust-remote-code" }, { toolCallParser: "../../parser" }]) {
    assert.throws(() => parseNewDeployment(form(values)))
  }
})

test("the real creation action sends the model repository and chosen stamp without issuer or gateway configuration", async () => {
  const calls = []
  const redirects = []
  const actions = compile("../app/(console)/actions.ts", {
    "node:crypto": { randomUUID: () => "idempotency-test" },
    "next/cache": { revalidatePath() {} },
    "next/navigation": { redirect: (path) => redirects.push(path) },
    "@/lib/fabric/new-deployment": { parseNewDeployment, parsePlacementStamp },
    "@/lib/fabric/client": { FabricApiError: class extends Error {}, async controlPlaneRequest(path, init) { calls.push({ path, init }); return { id: "deployment-one" } } },
    "@/lib/fabric/session": { getConsoleContext: async () => ({ account: { id: "tenant-one" } }), getFabricAccessToken: async () => "tenant-one-token", requireScope(_context, scope) { assert.equal(scope, "deployments:write") } },
  })
  await actions.createDeployment({}, form({ maxModelLen: "4096" }))
  assert.equal(calls.length, 2)
  const payload = JSON.parse(calls[0].init.body)
  assert.equal(payload.spec.runtime.release, "Qwen/Qwen3.5-4B")
  assert.equal(payload.spec.runtime.kernel_mode, "standard")
  assert.equal(payload.spec.runtime.max_model_len, 4096)
  assert.equal(payload.model_alias, "qwen3.5-4b")
  assert.deepEqual(JSON.parse(calls[1].init.body), { stamp_id: stampId })
  assert.ok(calls.every(({ path, init }) => path.startsWith("/v1/accounts/tenant-one/") && init.token === "tenant-one-token"))
  assert.deepEqual(redirects, ["/deployments/deployment-one"])
})

test("the creation action preserves explicit disabled tool calling and sends a selected parser", async () => {
  const bodies = []
  const actions = compile("../app/(console)/actions.ts", {
    "node:crypto": { randomUUID: () => "idempotency-test" },
    "next/cache": { revalidatePath() {} },
    "next/navigation": { redirect() {} },
    "@/lib/fabric/new-deployment": { parseNewDeployment, parsePlacementStamp },
    "@/lib/fabric/client": { FabricApiError: class extends Error {}, async controlPlaneRequest(path, init) { if (!path.endsWith("/placements")) bodies.push(JSON.parse(init.body)); return { id: "deployment-one" } } },
    "@/lib/fabric/session": { getConsoleContext: async () => ({ account: { id: "tenant-one" } }), getFabricAccessToken: async () => "tenant-one-token", requireScope() {} },
  })
  await actions.createDeployment({}, form({ autoToolChoice: "disabled" }))
  assert.equal(bodies[0].spec.runtime.enable_auto_tool_choice, false)
  assert.equal(Object.hasOwn(bodies[0].spec.runtime, "tool_call_parser"), false)
  await actions.createDeployment({}, form({ autoToolChoice: "enabled", toolCallParser: "qwen3_xml" }))
  assert.equal(bodies[1].spec.runtime.enable_auto_tool_choice, true)
  assert.equal(bodies[1].spec.runtime.tool_call_parser, "qwen3_xml")
  await actions.createDeployment({}, form())
  assert.equal(Object.hasOwn(bodies[2].spec.runtime, "enable_auto_tool_choice"), false)
  assert.equal(Object.hasOwn(bodies[2].spec.runtime, "tool_call_parser"), false)
})

test("placement retries read account-owned intent, preserve errors, and refuse deleted or foreign deployments", async () => {
  const calls = []
  let record = { id: "deployment-one", account_id: "tenant-one", status: "pending", deleted_at: null }
  class FabricApiError extends Error {}
  let capacityError = false
  const actions = compile("../app/(console)/actions.ts", {
    "node:crypto": { randomUUID: () => "idempotency-test" },
    "next/cache": { revalidatePath() {} },
    "next/navigation": { redirect() {} },
    "@/lib/fabric/new-deployment": { parseNewDeployment, parsePlacementStamp },
    "@/lib/fabric/client": { FabricApiError, async controlPlaneRequest(path, init) { calls.push({ path, init }); if (init.method === "POST" && capacityError) throw new FabricApiError("No compatible GPU capacity is available"); return record } },
    "@/lib/fabric/session": { getConsoleContext: async () => ({ account: { id: "tenant-one" } }), getFabricAccessToken: async () => "tenant-one-token", requireScope() {} },
  })
  const result = await actions.assignDeployment("deployment-one", {}, form())
  assert.equal(result.ok, true)
  assert.equal(calls.length, 2)
  assert.deepEqual(JSON.parse(calls[1].init.body), { stamp_id: stampId })
  capacityError = true
  assert.match((await actions.assignDeployment("deployment-one", {}, form())).error, /No compatible GPU/)
  for (const replacement of [{ ...record, account_id: "another-tenant" }, { ...record, status: "terminating" }, { ...record, status: "deleted" }]) {
    calls.length = 0
    record = replacement
    assert.equal((await actions.assignDeployment("deployment-one", {}, form())).error, "This deployment cannot be assigned.")
    assert.equal(calls.length, 1)
  }
})

test("invalid model form returns a reviewable error before creating any deployment", async () => {
  const actions = compile("../app/(console)/actions.ts", {
    "node:crypto": { randomUUID() {} },
    "next/cache": { revalidatePath() {} },
    "next/navigation": { redirect() {} },
    "@/lib/fabric/new-deployment": { parseNewDeployment, parsePlacementStamp },
    "@/lib/fabric/client": { FabricApiError: class extends Error {}, controlPlaneRequest() { throw new Error("Must not call control plane") } },
    "@/lib/fabric/session": { getConsoleContext: async () => ({ account: { id: "tenant-one" } }), getFabricAccessToken: async () => "token", requireScope() {} },
  })
  assert.match((await actions.createDeployment({}, form({ modelRef: "../private" }))).error, /model repository/)
})

test("a lost placement response still opens the saved deployment for review and retry", async () => {
  const redirects = []
  let creations = 0
  const actions = compile("../app/(console)/actions.ts", {
    "node:crypto": { randomUUID: () => "idempotency-test" },
    "next/cache": { revalidatePath() {} },
    "next/navigation": { redirect: (path) => redirects.push(path) },
    "@/lib/fabric/new-deployment": { parseNewDeployment, parsePlacementStamp },
    "@/lib/fabric/client": { FabricApiError: class extends Error {}, async controlPlaneRequest(path) { if (path.endsWith("/placements")) throw new TypeError("fetch failed"); creations++; return { id: "saved-deployment" } } },
    "@/lib/fabric/session": { getConsoleContext: async () => ({ account: { id: "tenant-one" } }), getFabricAccessToken: async () => "token", requireScope() {} },
  })
  await actions.createDeployment({}, form())
  assert.equal(creations, 1)
  assert.deepEqual(redirects, ["/deployments/saved-deployment?placement=failed"])
})
