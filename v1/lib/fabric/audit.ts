// Audit-trail query building and presentation. Kept free of server-only imports so the
// page, the export route, and the tests share one definition of a valid query.

export const AUDIT_PAGE_SIZE = 50

// Actions the control plane records today, with the label the console shows.
export const AUDIT_ACTIONS: Record<string, string> = {
  "account.bootstrapped": "Account bootstrapped",
  "account.created": "Account created",
  "account.managed_capacity_set": "Managed capacity changed",
  "api_key.created": "API key created",
  "api_key.revoked": "API key revoked",
  "audit.exported": "Audit trail exported",
  "deployment.created": "Deployment created",
  "deployment.deleted": "Deployment deleted",
  "deployment.updated": "Deployment updated",
  "membership.upserted": "Member added or changed",
  "oidc_provider.configured": "Identity provider configured",
  "oidc_provider.removed": "Identity provider removed",
  "placement.created": "Deployment placed",
  "service_principal.created": "Service principal created",
  "service_principal.disabled": "Service principal disabled",
  "stamp.credentials_rotated": "Stamp credentials rotated",
  "stamp.enrolled": "Stamp enrolled",
  "stamp.revoked": "Stamp revoked",
  "stamp_enrollment_token.created": "Enrollment token created",
  "stamp_enrollment_token.revoked": "Enrollment token revoked",
  "user.created": "User created",
}

export type AuditSearch = { action?: string; resource?: string; cursor?: string }

type SearchParams = Record<string, string | string[] | undefined>

function single(value: string | string[] | undefined) {
  return typeof value === "string" ? value.trim() : undefined
}

// Only known actions and bounded, printable identifiers are forwarded, so a crafted URL
// cannot smuggle extra query parameters to the control plane.
export function parseAuditSearch(params: SearchParams): AuditSearch {
  const action = single(params.action)
  const resource = single(params.resource)
  const cursor = single(params.cursor)
  return {
    action: action && Object.hasOwn(AUDIT_ACTIONS, action) ? action : undefined,
    resource: resource && /^[A-Za-z0-9._:-]{1,128}$/.test(resource) ? resource : undefined,
    cursor: cursor && /^[A-Za-z0-9_-]{1,512}$/.test(cursor) ? cursor : undefined,
  }
}

export function auditQuery(search: AuditSearch, extra: Record<string, string> = {}) {
  const query = new URLSearchParams()
  if (search.action) query.set("action", search.action)
  if (search.resource) query.set("resource_id", search.resource)
  if (search.cursor) query.set("cursor", search.cursor)
  for (const [key, value] of Object.entries(extra)) query.set(key, value)
  return query
}

// Console URL for the same view, used by filter, paging and export links.
export function auditHref(path: string, search: AuditSearch) {
  const query = new URLSearchParams()
  if (search.action) query.set("action", search.action)
  if (search.resource) query.set("resource", search.resource)
  if (search.cursor) query.set("cursor", search.cursor)
  const encoded = query.toString()
  return encoded ? `${path}?${encoded}` : path
}

export function auditActionLabel(action: string) {
  return AUDIT_ACTIONS[action] ?? action
}

// One line summarising an event's metadata. Values are shown as recorded; the control
// plane never writes credentials, prompts or responses into audit metadata.
export function summarizeAuditMetadata(metadata: Record<string, unknown>) {
  return Object.entries(metadata)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join(" · ")
}
