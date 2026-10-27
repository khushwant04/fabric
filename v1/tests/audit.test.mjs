import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function compile(relative) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}
  vm.runInNewContext(code, { exports, URLSearchParams, require(name) { throw new Error(`Unexpected dependency: ${name}`) } })
  return exports
}

const audit = compile("../lib/fabric/audit.ts")

test("only known actions and bounded identifiers reach the control plane", () => {
  const search = audit.parseAuditSearch({ action: "api_key.created", resource: "4c0a0d13-70e5", cursor: "eyJ0IjoiMjAyNiJ9" })
  assert.equal(search.action, "api_key.created")
  assert.equal(search.resource, "4c0a0d13-70e5")
  assert.equal(search.cursor, "eyJ0IjoiMjAyNiJ9")

  const hostile = audit.parseAuditSearch({ action: "api_key.created&limit=100000", resource: "x&actor_id=y", cursor: "a=b&c" })
  assert.deepEqual({ ...hostile }, { action: undefined, resource: undefined, cursor: undefined })
  assert.equal(audit.auditQuery(hostile).toString(), "")
})

test("repeated parameters are ignored rather than joined", () => {
  assert.equal(audit.parseAuditSearch({ action: ["api_key.created", "api_key.revoked"] }).action, undefined)
})

test("console filters map onto the control-plane query names", () => {
  const query = audit.auditQuery({ action: "stamp.revoked", resource: "stamp-1", cursor: "next" }, { limit: "50" })
  assert.equal(query.get("action"), "stamp.revoked")
  assert.equal(query.get("resource_id"), "stamp-1")
  assert.equal(query.get("cursor"), "next")
  assert.equal(query.get("limit"), "50")
  assert.equal(query.has("resource"), false)
})

test("console links keep filters and drop empty values", () => {
  assert.equal(audit.auditHref("/audit", {}), "/audit")
  assert.equal(audit.auditHref("/audit", { action: "api_key.created", resource: undefined }), "/audit?action=api_key.created")
  assert.equal(audit.auditHref("/api/audit/export", { resource: "abc", cursor: "c1" }), "/api/audit/export?resource=abc&cursor=c1")
})

test("labels and metadata summaries are readable", () => {
  assert.equal(audit.auditActionLabel("audit.exported"), "Audit trail exported")
  assert.equal(audit.auditActionLabel("future.action"), "future.action")
  assert.equal(audit.summarizeAuditMetadata({ events: 3, filters: { action: "x" }, empty: "", gone: null }), 'events: 3 · filters: {"action":"x"}')
})
