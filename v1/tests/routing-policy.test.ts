import { strict as assert } from "node:assert"
import { test } from "node:test"

import {
  addTarget,
  aliasProblem,
  describeMatch,
  diffDocuments,
  emptyDocument,
  evenWeights,
  flowLayout,
  moveItem,
  newRule,
  normalizeDocument,
  parseHeaderLines,
  parseOptionalInt,
  removeTarget,
  sameDocument,
  setWeight,
  weightTotal,
  type PolicyDocument,
  type RuleTarget,
} from "../lib/fabric/routing-policy"

const targets = (...weights: number[]): RuleTarget[] => weights.map((weight, index) => ({ deployment_id: `d${index}`, weight }))

test("weights always sum to 100 whatever the edit", () => {
  assert.deepEqual(evenWeights(3), [34, 33, 33])
  for (const start of [targets(100), targets(50, 50), targets(70, 20, 10), targets(0, 100, 0), targets(34, 33, 33)]) {
    for (let index = 0; index < start.length; index += 1) {
      for (const weight of [-5, 0, 1, 33, 50, 99, 100, 140, Number.NaN]) {
        const next = setWeight(start, index, weight)
        assert.equal(weightTotal(next), 100, `${JSON.stringify(start)} [${index}] = ${weight}`)
        assert.ok(next.every((target) => target.weight >= 0 && Number.isInteger(target.weight)))
      }
    }
  }
  // The others keep their proportions.
  assert.deepEqual(setWeight(targets(60, 30, 10), 0, 20).map((t) => t.weight), [20, 60, 20])
  assert.deepEqual(setWeight(targets(100), 0, 40).map((t) => t.weight), [100])
})

test("adding a target starts a canary and removing one rescales the rest", () => {
  const added = addTarget(targets(100), "canary")
  assert.deepEqual(added.map((t) => t.weight), [90, 10])
  assert.equal(addTarget(added, "canary"), added, "a deployment is never added twice")
  assert.deepEqual(addTarget([], "only"), [{ deployment_id: "only", weight: 100 }])
  const removed = removeTarget(targets(60, 30, 10), 2)
  assert.equal(weightTotal(removed), 100)
  assert.deepEqual(removed.map((t) => t.weight), [67, 33])
  assert.deepEqual(removeTarget(targets(0, 0, 100), 2).map((t) => t.weight), [50, 50])
})

test("rules move, and new ones get unique ids", () => {
  assert.deepEqual(moveItem(["a", "b", "c"], 0, 2), ["b", "c", "a"])
  assert.deepEqual(moveItem(["a", "b"], 0, 5), ["a", "b"])
  const rule = newRule(["rule-1", "default"], targets(100))
  assert.equal(rule.id, "rule-3")
  assert.deepEqual(rule.fallback_on, ["server_error", "no_ready_host"])
})

test("conditions read as short phrases", () => {
  assert.deepEqual(describeMatch({}), [])
  assert.deepEqual(
    describeMatch({ endpoints: ["messages"], has_tools: true, headers: [{ name: "x-tier", op: "equals", value: "gold" }, { name: "x-app", op: "regex", value: "^batch" }], prompt_tokens: { min: 8000 }, max_tokens: { max: 256 }, api_key_ids: ["k1"] }, { keys: { k1: "batch key" } }),
    ["Anthropic messages", "has tools", "key batch key", "x-tier = gold", "x-app ~ /^batch/", "prompt ≥ 8,000", "max_tokens ≤ 256"]
  )
})

const live: PolicyDocument = {
  rules: [{ id: "tools", name: "Tools", match: { has_tools: true }, targets: [{ deployment_id: "big", weight: 100 }], fallbacks: [], fallback_on: ["server_error"] }],
  default: { ...emptyDocument("stable").default },
}

test("the publish diff names what changes", () => {
  assert.deepEqual(diffDocuments(live, live), [])
  const draft: PolicyDocument = {
    rules: [
      { id: "long", name: "Long prompts", match: { prompt_tokens: { min: 8000 } }, targets: [{ deployment_id: "big", weight: 100 }], fallbacks: [], fallback_on: [] },
      { ...live.rules[0], fallbacks: ["stable"] },
    ],
    default: { ...live.default, targets: [{ deployment_id: "stable", weight: 90 }, { deployment_id: "canary", weight: 10 }] },
  }
  const names = new Map([["big", "chat-big"], ["stable", "chat-stable"], ["canary", "chat-canary"]])
  const diff = diffDocuments(live, draft, (id) => names.get(id) ?? id)
  assert.deepEqual(diff.map((entry) => [entry.kind, entry.rule]), [["added", "Long prompts"], ["changed", "Tools"], ["changed", "Default"]])
  assert.match(diff[1].detail, /fallbacks none → chat-stable/)
  assert.match(diff[2].detail, /split chat-stable 100% → chat-stable 90%, chat-canary 10%/)
  const reordered = diffDocuments({ ...draft }, { ...draft, rules: [draft.rules[1], draft.rules[0]] })
  assert.deepEqual(reordered.map((entry) => entry.kind), ["moved"])
  assert.equal(diffDocuments(null, draft)[0].kind, "added")
  assert.equal(diffDocuments(draft, live).some((entry) => entry.kind === "removed"), true)
})

test("normalising ignores empty optional fields", () => {
  const sparse: PolicyDocument = { rules: [{ id: "r", name: "R", match: { headers: [] }, targets: [{ deployment_id: "a", weight: 100 }], fallbacks: [], fallback_on: ["no_ready_host", "server_error"] }], default: live.default }
  const full = normalizeDocument(sparse)
  assert.deepEqual(full.rules[0].fallback_on, ["server_error", "no_ready_host"])
  assert.equal(full.rules[0].match.prompt_tokens, null)
  assert.ok(sameDocument(sparse, full))
  assert.ok(!sameDocument(sparse, live))
})

test("the flow lays out the alias, rules in order and each deployment once", () => {
  const draft: PolicyDocument = { ...live, default: { ...live.default, targets: [{ deployment_id: "stable", weight: 80 }, { deployment_id: "big", weight: 20 }], fallbacks: ["canary"] } }
  const { nodes, edges } = flowLayout("chat-default", 3, draft, [
    { id: "stable", name: "chat-stable", alias: "s", ready: true, tools: false },
    { id: "big", name: "chat-big", alias: "b", ready: false, tools: true },
  ])
  assert.deepEqual(nodes.map((node) => node.id), ["alias", "rule:tools", "rule:default", "dep:big", "dep:stable", "dep:canary"])
  const rules = nodes.filter((node) => node.kind === "rule")
  assert.ok(rules[0].y < rules[1].y, "the default rule is drawn last")
  const unknown = nodes.find((node) => node.id === "dep:canary")
  assert.ok(unknown && unknown.kind === "target" && !unknown.known)
  assert.deepEqual(edges.filter((edge) => edge.kind === "split" && edge.ruleId === "default").map((edge) => edge.label), ["80%", "20%"])
  assert.deepEqual(edges.filter((edge) => edge.kind === "fallback").map((edge) => edge.target), ["dep:canary"])
})

test("form inputs", () => {
  assert.equal(parseOptionalInt(""), null)
  assert.equal(parseOptionalInt("12"), 12)
  assert.equal(parseOptionalInt("-3"), null)
  assert.equal(parseOptionalInt("1.5"), null)
  assert.deepEqual(parseHeaderLines("x-tier: gold\nbad line\n x-app : batch:nightly "), { "x-tier": "gold", "x-app": "batch:nightly" })
  assert.equal(aliasProblem("chat-default", ["chat-stable"]), null)
  assert.match(aliasProblem("chat-stable", ["chat-stable"]) ?? "", /already answers/)
  assert.match(aliasProblem("auto", []) ?? "", /reserved/)
  assert.match(aliasProblem("has space", []) ?? "", /Use letters/)
})
