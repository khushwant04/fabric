import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative, dependencies = {}, globals = {}) {
  const exports = {}
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  vm.runInNewContext(code, { exports, URL, ...globals, require(name) {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency ${name}`)
    return dependencies[name]
  } })
  return exports
}

const state = compile("../lib/fabric/resource-state.ts")
const playground = compile("../lib/fabric/playground.ts", { "./resource-state": state })
const deployment = {
  id: "dep-qwen", account_id: "account-one", name: "Qwen deployment", model_alias: "qwen",
  status: "ready", generation: 1, desired_spec: { replicas: 1 },
}
const placement = {
  id: "placement-one", account_id: "account-one", deployment_id: deployment.id,
  stamp_id: "stamp-one", status: "ready", desired_generation: 14, observed_generation: 14,
}
const status = {
  deployment_id: deployment.id, stamp_id: placement.stamp_id, phase: "ready",
  observed_generation: 14, ready_replicas: 1, unavailable_replicas: 0,
  endpoint: "https://inference.hexelstudio.com", reported_at: "2026-10-06T16:34:38Z",
  conditions: [{ type: "Available", status: "True", reason: "ModelHostServing" }],
}

function fixture({ env = {}, placements = [placement], statuses = [status], allowed = true } = {}) {
  let reads = 0
  const dataModule = compile("../lib/fabric/playground-data.ts", {
    "server-only": {},
    "@/lib/fabric/playground": playground,
    "@/lib/fabric/session": {
      getConsoleContext: async () => ({ account: { id: deployment.account_id } }),
      hasScope: (_context, scope) => allowed && scope === "deployments:read",
    },
    "@/lib/fabric/data": {
      listDeployments: async () => { reads++; return [deployment] },
      listPlacements: async (id) => { assert.equal(id, deployment.id); return placements },
      getDeploymentStatuses: async (id) => { assert.equal(id, deployment.id); return statuses },
    },
  }, { process: { env: { NODE_ENV: "production", ...env } } })
  return { run: dataModule.getPlaygroundOptions, reads: () => reads }
}

test("a ready placement at generation 14 is offered through the exactly approved gateway", async () => {
  const item = fixture({ env: { FABRIC_INFERENCE_ALLOWED_ORIGINS: status.endpoint } })
  const result = await item.run()
  assert.equal(result.notice, null)
  assert.deepEqual(Array.from(result.options, (option) => option.id), ["virtual-auto", deployment.id])
  assert.equal(result.options[1].modelAlias, "qwen")
  assert.equal(result.options[1].deploymentId, deployment.id)
})

test("missing gateway configuration and unapproved reported endpoints fail closed", async () => {
  for (const overrides of [
    {},
    { env: { FABRIC_INFERENCE_ALLOWED_ORIGINS: "https://another.example.com" } },
    { env: { FABRIC_INFERENCE_ALLOWED_ORIGINS: status.endpoint }, statuses: [{ ...status, endpoint: "https://inference.hexelstudio.com.attacker.example.com" }] },
  ]) {
    const result = await fixture(overrides).run()
    assert.equal(result.options.length, 0)
    assert.match(result.notice, /No ready model/)
  }
})

test("approved gateways cannot make unready, stale-generation, or withdrawn placements selectable", async () => {
  const env = { FABRIC_INFERENCE_URL: status.endpoint }
  for (const overrides of [
    { statuses: [] },
    { statuses: [{ ...status, observed_generation: 1 }] },
    { statuses: [{ ...status, ready_replicas: 0, unavailable_replicas: 1 }] },
    { statuses: [{ ...status, conditions: [{ type: "Available", status: "False" }] }] },
    { placements: [{ ...placement, status: "revoked" }] },
    { placements: [{ ...placement, observed_generation: 13 }] },
  ]) {
    const result = await fixture({ env, ...overrides }).run()
    assert.equal(result.options.length, 0)
  }
})

test("roles without deployment access do not fetch model inventory", async () => {
  const item = fixture({ allowed: false })
  const result = await item.run()
  assert.equal(result.options.length, 0)
  assert.equal(item.reads(), 0)
  assert.match(result.notice, /role does not allow/)
})

function fleet({ env = {}, scopes = ["deployments:read", "stamps:read"] } = {}) {
  // Two stamps: a and b on stamp A, c on stamp B.
  const make = (id, alias, stampId, endpoint) => ({
    deployment: { ...deployment, id, name: `${alias} deployment`, model_alias: alias },
    placement: { ...placement, id: `placement-${id}`, deployment_id: id, stamp_id: stampId },
    status: { ...status, deployment_id: id, stamp_id: stampId, endpoint },
  })
  const items = [
    make("dep-a", "small", "stamp-a", "https://stamp-a.gw.example.com/v1"),
    make("dep-b", "medium", "stamp-a", "https://stamp-a.gw.example.com/v1"),
    make("dep-c", "large", "stamp-b", "https://stamp-b.gw.example.com/v1"),
  ]
  const byId = new Map(items.map((item) => [item.deployment.id, item]))
  const dataModule = compile("../lib/fabric/playground-data.ts", {
    "server-only": {},
    "@/lib/fabric/playground": playground,
    "@/lib/fabric/session": {
      getConsoleContext: async () => ({ account: { id: deployment.account_id } }),
      hasScope: (_context, scope) => scopes.includes(scope),
    },
    "@/lib/fabric/data": {
      listDeployments: async () => items.map((item) => item.deployment),
      listPlacements: async (id) => [byId.get(id).placement],
      getDeploymentStatuses: async (id) => [byId.get(id).status],
      listStamps: async () => [{ id: "stamp-a", name: "aks-gpu", capabilities: {} }, { id: "stamp-b", name: "gcp-rtx6000", capabilities: {} }],
    },
  }, { process: { env: { NODE_ENV: "production", ...env } } })
  return dataModule
}

test("deployments on several stamps are each offered, and auto goes to the stamp serving most of them", async () => {
  const { getPlaygroundOptions } = fleet({ env: { FABRIC_INFERENCE_URL: "https://stamp-a.gw.example.com", FABRIC_INFERENCE_ALLOWED_DOMAINS: "gw.example.com" } })
  const result = await getPlaygroundOptions()
  assert.deepEqual(Array.from(result.options, (option) => option.id), ["virtual-auto", "dep-a", "dep-b", "dep-c"])
  assert.equal(result.options[0].deploymentId, "dep-a")
  assert.equal(result.options[0].label, "Auto · model selection on aks-gpu")
})

test("with the edge gateway every deployment shares one gateway, and the edge is reported for the cards", async () => {
  const env = { FABRIC_EDGE_INFERENCE_URL: "https://edge.example.com", FABRIC_INFERENCE_URL: "https://edge.example.com" }
  const data = fleet({ env })
  const result = await data.getPlaygroundOptions()
  assert.deepEqual(Array.from(result.options, (option) => option.id), ["virtual-auto", "dep-a", "dep-b", "dep-c"])
  assert.equal(result.options[0].label, "Auto · gateway model selection")
  assert.equal(data.edgeGatewayUrl(), "https://edge.example.com/v1")
  assert.equal(data.consoleUsesEdge(), true)
  const separate = fleet({ env: { FABRIC_EDGE_INFERENCE_URL: "https://edge.example.com", FABRIC_INFERENCE_URL: "https://stamp-a.gw.example.com" } })
  assert.equal(separate.consoleUsesEdge(), false)
  assert.equal(fleet().edgeGatewayUrl(), null)
  assert.equal(fleet({ env: { FABRIC_EDGE_INFERENCE_URL: "http://edge.example.com" } }).edgeGatewayUrl(), null)
})
