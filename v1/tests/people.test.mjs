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

const format = compile("../lib/format.ts")
const { accountLabel, memberIdentity, initials } = compile("../lib/fabric/people.ts", { "@/lib/format": format })
const plain = (value) => JSON.parse(JSON.stringify(value))
const accountId = "020cf926-1111-4222-8333-444455556666"

test("accounts are labelled by name, falling back to a short id for an older control plane", () => {
  assert.equal(accountLabel({ account_id: accountId, account_name: "Northstar Labs" }), "Northstar Labs")
  assert.equal(accountLabel({ account_id: accountId, account_name: "  " }), "Account 020cf926…6666")
  assert.equal(accountLabel({ account_id: accountId }), "Account 020cf926…6666")
})

test("members show their name and email, the signed-in user's own profile, or that they have not signed in", () => {
  const me = { id: "user-me", display_name: "Maya Chen", email: "maya@example.test" }
  assert.deepEqual(plain(memberIdentity({ user_id: "user-2", display_name: "Priya Raman", email: "priya@example.test" }, me)), { name: "Priya Raman", detail: "priya@example.test", self: false, pending: false })
  assert.deepEqual(plain(memberIdentity({ user_id: "user-3", display_name: null, email: "invited@example.test" }, me)), { name: "invited@example.test", detail: null, self: false, pending: false })
  assert.deepEqual(plain(memberIdentity({ user_id: "user-4", display_name: null, email: null }, me)), { name: "Not signed in yet", detail: null, self: false, pending: true })
  // An older control plane returns ids only; the signed-in user is still named.
  assert.deepEqual(plain(memberIdentity({ user_id: "user-me" }, me)), { name: "Maya Chen", detail: "maya@example.test", self: true, pending: false })
})

test("initials come from the name, or the local part of an email", () => {
  assert.equal(initials("Maya Chen"), "MC")
  assert.equal(initials("Sofia Anna Lindqvist"), "SL")
  assert.equal(initials("tom.becker@example.test"), "TB")
  assert.equal(initials("ops"), "OP")
})
