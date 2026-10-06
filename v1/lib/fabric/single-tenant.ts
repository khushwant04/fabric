import "server-only"

// Read at request time so one image works for managed and self-hosted installs.
// A configured workspace never comes from a browser cookie.
export function getSingleTenantAccountId(): string | null {
  const accountId = process.env.FABRIC_SINGLE_TENANT_ACCOUNT_ID?.trim()
  if (!accountId) return null
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(accountId)) {
    throw new Error("FABRIC_SINGLE_TENANT_ACCOUNT_ID must be an account UUID")
  }
  // PostgreSQL and the control-plane UUID schema return the canonical lower case.
  return accountId.toLowerCase()
}

export function requireAccountSelectionEnabled() {
  if (getSingleTenantAccountId()) {
    throw new Error("This installation uses the workspace configured by its administrator")
  }
}

export function requireConfiguredAccount(accountId: string) {
  const configuredAccountId = getSingleTenantAccountId()
  if (configuredAccountId && accountId.toLowerCase() !== configuredAccountId) {
    throw new Error("This account is outside the configured workspace")
  }
}
