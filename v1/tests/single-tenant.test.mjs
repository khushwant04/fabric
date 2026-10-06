import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import vm from "node:vm"
import ts from "typescript"

function tenantModule(accountId) {
  const exports = {}
  const env = accountId === undefined ? {} : { FABRIC_SINGLE_TENANT_ACCOUNT_ID: accountId }
  const code = ts.transpileModule(readFileSync(new URL("../lib/fabric/single-tenant.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  vm.runInNewContext(code, { exports, process: { env }, require: (name) => { assert.equal(name, "server-only"); return {} } })
  return exports
}

test("an unset or blank workspace keeps managed mode and malformed configuration fails closed", () => {
  for (const id of [undefined, "", "  "]) {
    const tenant = tenantModule(id)
    assert.equal(tenant.getSingleTenantAccountId(), null)
    assert.doesNotThrow(() => tenant.requireAccountSelectionEnabled())
  }
  for (const id of ["workspace-slug", "../../account", "4c0a0d13-70e5-4a98-9a61-0a278fdc831"]) {
    const tenant = tenantModule(id)
    assert.throws(() => tenant.getSingleTenantAccountId(), /must be an account UUID/)
    assert.throws(() => tenant.requireAccountSelectionEnabled(), /must be an account UUID/)
  }
})

test("the fixed account is read from server configuration and only that account passes", () => {
  const account = "4c0a0d13-70e5-4a98-9a61-0a278fdc831c"
  const tenant = tenantModule(` ${account} `)
  assert.equal(tenant.getSingleTenantAccountId(), account)
  assert.doesNotThrow(() => tenant.requireConfiguredAccount(account))
  assert.throws(() => tenant.requireConfiguredAccount("other-account"), /outside the configured workspace/)
})

test("uppercase Helm UUID configuration matches canonical control-plane account ids", () => {
  const account = "4c0a0d13-70e5-4a98-9a61-0a278fdc831c"
  const tenant = tenantModule(` ${account.toUpperCase()} `)
  assert.equal(tenant.getSingleTenantAccountId(), account)
  assert.doesNotThrow(() => tenant.requireConfiguredAccount(account))
  assert.doesNotThrow(() => tenant.requireConfiguredAccount(account.toUpperCase()))
  assert.throws(() => tenant.requireConfiguredAccount("4c0a0d13-70e5-4a98-9a61-0a278fdc831d"), /outside the configured workspace/)
})
