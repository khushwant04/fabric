import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative, dependencies = {}, globals = {}) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, URL, Response, Headers, TextDecoder, TextEncoder, ReadableStream, AbortController, AbortSignal, setTimeout, clearTimeout, ...globals, require: (name) => {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency ${name}`)
    return dependencies[name]
  } })
  return exports
}

const state = compile("../lib/fabric/resource-state.ts")
const playground = compile("../lib/fabric/playground.ts", { "./resource-state": state })
const runs = compile("../lib/fabric/agent-runs.ts", { "./playground": playground })
const agent = compile("../lib/fabric/agent-playground.ts", { "./agent-runs": runs })

const body = { deploymentId: "dep-one", model: "exact", messages: [{ role: "user", content: "Split 86.40 EUR three ways." }], tools: ["calculator", "code_interpreter"], maxSteps: 6, budgetTokens: 4000 }
const deployment = { id: "dep-one", account_id: "account-one", model_alias: "small-model", status: "ready", desired_spec: { replicas: 1 } }
const placement = { id: "placement-one", account_id: "account-one", deployment_id: "dep-one", stamp_id: "stamp-one", status: "active", desired_generation: 7, observed_generation: 7 }
const status = { deployment_id: "dep-one", stamp_id: "stamp-one", phase: "ready", observed_generation: 7, ready_replicas: 1, unavailable_replicas: 0, conditions: [], reported_at: "2026-10-06T12:00:00Z" }
const events = 'event: run.started\ndata: {"id":"run_1","model":"small-model"}\n\nevent: run.completed\ndata: {"id":"run_1","status":"completed"}\n\n'

function fixture({ authenticated = true, upstreamStatus = 200, upstreamBody = null, ownsCascade = false } = {}) {
  const calls = []
  let canceled = false
  class FabricApiError extends Error {
    constructor(status) { super("private control-plane detail"); this.status = status }
  }
  const route = compile("../app/api/agents/runs/route.ts", {
    "@/lib/fabric/client": { FabricApiError, controlPlaneRequest: async (path, init) => {
      assert.equal(init.token, "account-one-control-token")
      if (path === "/v1/accounts/account-one/deployments/dep-one") return deployment
      if (path === "/v1/accounts/account-one/deployments/dep-one/status") return [status]
      if (path === "/v1/accounts/account-one/deployments/dep-one/placements") return [placement]
      if (path === "/v1/accounts/account-one/deployments") return ownsCascade ? [deployment, { ...deployment, id: "dep-two", model_alias: "cascade" }] : [deployment]
      throw new Error(`Unexpected account-bound request ${path}`)
    } },
    "@/lib/fabric/agent-playground": agent,
    "@/lib/fabric/playground-data": { inferenceConfiguration: () => ({ configuredUrl: "https://inference.example.test", production: true }) },
    "@/lib/fabric/playground": playground,
    "@/lib/fabric/session": {
      getAuthSession: async () => authenticated ? {} : null,
      getConsoleContext: async () => ({ account: { id: "account-one" } }),
      hasScope: (_context, scope) => scope === "deployments:read",
      getFabricAccessToken: async () => "account-one-control-token",
      getFabricInferenceToken: async (account) => { assert.equal(account, "account-one"); return "account-one-inference-token" },
    },
  }, { process: { env: { APP_BASE_URL: "https://console.example.test" } }, fetch: async (url, init) => {
    calls.push({ url, init })
    if (upstreamStatus !== 200) return new Response(JSON.stringify(upstreamBody), { status: upstreamStatus, headers: { "Content-Type": "application/json" } })
    const stream = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(events)) }, cancel() { canceled = true } })
    return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream", "X-Fabric-Run-Id": "run_1", "X-Fabric-Cascade-Path": "small>large", "X-Private-Backend": "10.0.0.7" } })
  } })
  const run = (payload = body, { origin = "https://console.example.test", signal } = {}) => route.POST(new Request("https://console.example.test/api/agents/runs", { method: "POST", signal, headers: { "Content-Type": "application/json", Origin: origin }, body: JSON.stringify(payload) }))
  return { run, calls, canceled: () => canceled }
}

test("the route starts a streamed agent run on the approved gateway with the account's inference token", async () => {
  const item = fixture()
  const response = await item.run()
  assert.equal(response.status, 200)
  assert.equal(response.headers.get("Content-Type"), "text/event-stream; charset=utf-8")
  assert.equal(response.headers.get("X-Fabric-Cascade-Path"), "small>large")
  assert.equal(response.headers.get("X-Private-Backend"), null)
  const reader = response.body.getReader()
  const first = new TextDecoder().decode((await reader.read()).value)
  assert.match(first, /event: run.started/)
  await reader.cancel()
  assert.equal(item.canceled(), true, "closing the browser stream cancels the gateway stream")
  const { url, init } = item.calls[0]
  assert.equal(url, "https://inference.example.test/v1/agent/runs")
  assert.equal(init.redirect, "manual")
  assert.equal(init.headers.Authorization, "Bearer account-one-inference-token")
  assert.equal(init.headers["X-Fabric-Run-Budget-Tokens"], "4000")
  const sent = JSON.parse(init.body)
  assert.equal(sent.model, "small-model")
  assert.equal(sent.stream, true)
  assert.equal(sent.max_steps, 6)
  assert.deepEqual(sent.tools.map((tool) => tool["x-fabric-executor"].name), ["calculator", "code_interpreter"])
})

test("cross-origin, signed-out and malformed requests never reach the gateway", async () => {
  const item = fixture()
  assert.equal((await item.run(body, { origin: "https://evil.example" })).status, 403)
  assert.equal((await fixture({ authenticated: false }).run()).status, 401)
  for (const invalid of [{ ...body, url: "http://169.254.169.254" }, { ...body, maxSteps: 99 }, { ...body, tools: ["shell"] }, { ...body, model: "anything" }]) {
    const response = await item.run(invalid)
    assert.equal(response.status, 400)
    assert.equal(typeof (await response.json()).error, "string")
  }
  const large = await item.run({ ...body, messages: [{ role: "user", content: "x".repeat(1_100_000) }] })
  assert.equal(large.status, 413)
  assert.equal(item.calls.length, 0)
})

test("a virtual cascade is refused when the account owns a real cascade deployment", async () => {
  const response = await fixture({ ownsCascade: true }).run({ ...body, model: "cascade" })
  assert.equal(response.status, 400)
  assert.match((await response.json()).error, /exact cascade deployment/)
})

test("gateway refusals are mapped, and a disabled hosted tool names the tool so the page can switch it off", async () => {
  const disabled = await fixture({ upstreamStatus: 400, upstreamBody: { error: { code: "hosted_tool_disabled", message: "tool 'code_interpreter': hosted tool 'code_interpreter' is not enabled on this stamp" } } }).run()
  assert.equal(disabled.status, 400)
  assert.deepEqual(await disabled.json(), { error: "Code interpreter is not enabled on this stamp. Turn it off or ask an operator to enable it.", code: "hosted_tool_disabled", tool: "code_interpreter" })
  const old = await fixture({ upstreamStatus: 404, upstreamBody: { detail: "Not Found" } }).run()
  assert.equal(old.status, 501)
  assert.match((await old.json()).error, /does not support server-side agent runs/)
  const crash = await fixture({ upstreamStatus: 500, upstreamBody: { error: { message: "token=abc internal" } } }).run()
  assert.equal(crash.status, 502)
  assert.doesNotMatch(JSON.stringify(await crash.json()), /token=abc|account-one-inference-token/)
})
