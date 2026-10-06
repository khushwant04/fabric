import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative, dependencies = {}, globals = {}) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, URL, Response, Headers, TextDecoder, ReadableStream, AbortController, setTimeout, clearTimeout, ...globals, require: (name) => {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency ${name}`)
    return dependencies[name]
  } })
  return exports
}
const state = compile("../lib/fabric/resource-state.ts")
const playground = compile("../lib/fabric/playground.ts", { "./resource-state": state })
const body = { deploymentId: "dep-one", model: "exact", prompt: "Explain GPU scheduling.", maxTokens: 64, temperature: 0 }
const deployment = { id: "dep-one", account_id: "account-one", model_alias: "qwen3.5", status: "ready", desired_spec: { replicas: 1 } }
const placement = { id: "placement-one", account_id: "account-one", deployment_id: "dep-one", stamp_id: "stamp-one", status: "active", desired_generation: 7, observed_generation: 7 }
const status = { deployment_id: "dep-one", stamp_id: "stamp-one", phase: "ready", observed_generation: 7, ready_replicas: 1, unavailable_replicas: 0, conditions: [], reported_at: "2026-10-06T12:00:00Z" }

function fixture({ authenticated = true, write = true, foreign = false, ready = true, upstreamStatus = 200 } = {}) {
  const calls = []
  let canceled = false
  const route = compile("../app/api/playground/route.ts", {
    "@/lib/fabric/client": { FabricApiError: class extends Error {} },
    "@/lib/fabric/data": { getDeployment: async () => ({ ...deployment, ...(foreign ? { account_id: "other" } : {}) }), getDeploymentStatuses: async () => ready ? [status] : [], listPlacements: async () => [placement], listDeployments: async () => [deployment] },
    "@/lib/fabric/playground-data": { inferenceConfiguration: () => ({ configuredUrl: "https://inference.example.test", production: true }) },
    "@/lib/fabric/playground": playground,
    "@/lib/fabric/session": { getAuthSession: async () => authenticated ? {} : null, getConsoleContext: async () => ({ account: { id: "account-one" } }), hasScope: (_context, scope) => write && scope === "deployments:read", getFabricInferenceToken: async (account) => { assert.equal(account, "account-one"); return "account-one-inference-token" } },
  }, { process: { env: { APP_BASE_URL: "https://console.example.test" } }, fetch: async (url, init) => {
    calls.push({ url, init })
    const stream = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('data: {"choices":[]}\n\ndata: [DONE]\n\n')); controller.close() }, cancel() { canceled = true } })
    return new Response(upstreamStatus === 200 ? stream : null, { status: upstreamStatus, headers: { "Content-Type": "text/event-stream", "X-Fabric-Selected-Model": "qwen3.5", "X-Private-Backend": "internal" } })
  } })
  const run = (payload = body, origin = "https://console.example.test") => route.POST(new Request("https://console.example.test/api/playground", { method: "POST", headers: { "Content-Type": "application/json", Origin: origin }, body: JSON.stringify(payload) }))
  return { run, calls, canceled: () => canceled }
}

test("the BFF streams only to the approved gateway using the account inference token", async () => {
  const item = fixture()
  const response = await item.run({ ...body, task: "code" })
  assert.equal(response.status, 200)
  assert.match(await response.text(), /\[DONE\]/)
  assert.equal(item.calls.length, 1)
  const { url, init } = item.calls[0]
  assert.equal(url, "https://inference.example.test/v1/chat/completions")
  assert.equal(init.headers.Authorization, "Bearer account-one-inference-token")
  assert.equal(init.redirect, "manual")
  assert.deepEqual(JSON.parse(init.body), { model: "qwen3.5", messages: [{ role: "user", content: body.prompt }], max_tokens: 64, temperature: 0, stream: true, stream_options: { include_usage: true }, routing: { task: "code", explain: true } })
  assert.equal(response.headers.get("X-Fabric-Selected-Model"), "qwen3.5")
  assert.equal(response.headers.get("X-Private-Backend"), null)
})

test("cross-origin, missing session, foreign resources and scope failures never start inference", async () => {
  for (const [options, origin, expected] of [[{}, "https://attacker.example.test", 403], [{ authenticated: false }, undefined, 401], [{ foreign: true }, undefined, 404], [{ write: false }, undefined, 403]]) {
    const item = fixture(options)
    const response = await item.run(body, origin)
    assert.equal(response.status, expected)
    assert.equal(item.calls.length, 0)
  }
})

test("unready and malformed requests cannot reach the inference gateway", async () => {
  const unready = fixture({ ready: false })
  assert.equal((await unready.run()).status, 409)
  assert.equal(unready.calls.length, 0)
  const item = fixture()
  assert.equal((await item.run({ ...body, endpoint: "https://attacker.example.test" })).status, 400)
  assert.equal(item.calls.length, 0)
})

test("upstream redirects are rejected without following or forwarding credentials", async () => {
  const item = fixture({ upstreamStatus: 302 })
  const response = await item.run()
  assert.equal(response.status, 502)
  assert.equal(item.calls.length, 1)
  assert.equal(item.calls[0].init.redirect, "manual")
})
