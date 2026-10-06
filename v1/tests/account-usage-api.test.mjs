import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

// Execute the actual server data module with isolated control/session dependencies.
// The application keeps its server-only boundary; tests never import Auth0 secrets.
function compile(relative, dependencies = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), "utf8")
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const exports = {}
  vm.runInNewContext(code, {
    exports,
    require(name) {
      if (Object.hasOwn(dependencies, name)) return dependencies[name]
      throw new Error(`Unexpected dependency: ${name}`)
    },
  })
  return exports
}

const { summarizeAccountUsage } = compile("../lib/fabric/account-usage.ts")
const empty = { events: 0, input_tokens: 0, output_tokens: 0, first_occurred_at: null, last_occurred_at: null }
const account = "account-one"
const base = `/v1/accounts/${account}/deployments`
class ApiError extends Error {
  constructor(status) { super(`HTTP ${status}`); this.status = status }
}

function fixture({ aggregate, deployments = [{ id: "dep-one" }, { id: "dep-two" }], usage } = {}) {
  const calls = []
  let active = 0
  let peak = 0
  const rows = usage ?? (() => ({ ...empty, events: 2, input_tokens: 3, output_tokens: 4 }))
  const data = compile("../lib/fabric/data.ts", {
    "server-only": {},
    react: { cache: (fn) => fn },
    "@/lib/fabric/client": {
      FabricApiError: ApiError,
      async controlPlaneRequest(path, init) {
        calls.push(path)
        assert.equal(init.token, "account-one-control-token")
        if (path === `${base}/usage`) {
          if (aggregate instanceof Error) throw aggregate
          return aggregate ?? empty
        }
        if (path === base) {
          if (deployments instanceof Error) throw deployments
          return deployments
        }
        const deployment = deployments.find((item) => path === `${base}/${item.id}/usage`)
        assert.ok(deployment, `Unexpected account or deployment URL: ${path}`)
        active++
        peak = Math.max(peak, active)
        try { await new Promise((resolve) => setTimeout(resolve, 2)); return await rows(deployment.id) } finally { active-- }
      },
    },
    "@/lib/fabric/account-usage": { summarizeAccountUsage },
    "@/lib/fabric/session": {
      getConsoleContext: async () => ({ account: { id: account } }),
      getFabricAccessToken: async (id) => { assert.equal(id, account); return "account-one-control-token" },
    },
  })
  return { data, calls, peak: () => peak }
}

test("new aggregate endpoint is preferred without per-deployment calls", async () => {
  const expected = { ...empty, events: 42, output_tokens: 99 }
  const { data, calls } = fixture({ aggregate: expected })
  assert.deepEqual(await data.getAccountUsage(), expected)
  assert.deepEqual(calls, [`${base}/usage`])
})

for (const status of [404, 422]) {
  test(`older API HTTP ${status} falls back only to deployments listed for the same account`, async () => {
    const { data, calls } = fixture({ aggregate: new ApiError(status) })
    assert.deepEqual({ ...await data.getAccountUsage() }, { ...empty, events: 4, input_tokens: 6, output_tokens: 8 })
    assert.deepEqual(calls, [`${base}/usage`, base, `${base}/dep-one/usage`, `${base}/dep-two/usage`])
  })
}

for (const error of [new ApiError(401), new ApiError(403), new ApiError(500), new ApiError(503), new TypeError("network unavailable")]) {
  test(`${error.message} propagates without fallback or zero totals`, async () => {
    const { data, calls } = fixture({ aggregate: error })
    await assert.rejects(data.getAccountUsage(), (caught) => caught === error)
    assert.deepEqual(calls, [`${base}/usage`])
  })
}

test("fallback bounds parallel requests and preserves all totals", async () => {
  const { data, peak } = fixture({ aggregate: new ApiError(404), deployments: Array.from({ length: 11 }, (_, index) => ({ id: `dep-${index}` })) })
  assert.deepEqual({ ...await data.getAccountUsage() }, { ...empty, events: 22, input_tokens: 33, output_tokens: 44 })
  assert.ok(peak() <= 4)
  assert.equal(peak(), 4)
})

test("failed deployment or deployment-list reads propagate instead of undercounting", async () => {
  const error = new ApiError(503)
  const missingList = fixture({ aggregate: new ApiError(404), deployments: error })
  await assert.rejects(missingList.data.getAccountUsage(), (caught) => caught === error)
  const failedUsage = fixture({ aggregate: new ApiError(422), usage: (id) => { if (id === "dep-two") throw error; return { ...empty, events: 9 } } })
  await assert.rejects(failedUsage.data.getAccountUsage(), (caught) => caught === error)
})

test("an empty account preserves zero usage from its control-plane reads", async () => {
  const emptyAccount = fixture({ aggregate: new ApiError(404), deployments: [] })
  assert.deepEqual({ ...await emptyAccount.data.getAccountUsage() }, empty)
  assert.deepEqual(emptyAccount.calls, [`${base}/usage`, base])
})

test("summary finds chronological bounds across offsets and ignores absent timestamps", () => {
  const rows = [
    { ...empty, events: 3, input_tokens: 7, output_tokens: 2, first_occurred_at: "2026-10-01T01:00:00+02:00", last_occurred_at: "2026-10-01T05:00:00+02:00" },
    { ...empty, events: 2, input_tokens: 5, output_tokens: 9, first_occurred_at: "2026-10-01T00:00:00Z", last_occurred_at: "2026-10-01T04:00:00Z" },
    { ...empty },
  ]
  assert.deepEqual({ ...summarizeAccountUsage(rows) }, {
    events: 5, input_tokens: 12, output_tokens: 11,
    first_occurred_at: "2026-10-01T01:00:00+02:00", last_occurred_at: "2026-10-01T04:00:00Z",
  })
})
