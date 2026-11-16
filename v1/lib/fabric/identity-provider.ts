/**
 * Validation for the account identity provider form (`PUT /v1/accounts/{id}/oidc-provider`).
 * The control plane checks everything again; this only turns a mistyped field into a
 * message next to the form instead of a round trip.
 */

export type IdentityProviderInput = {
  issuer: string
  audience: string
  jwks_uri: string | null
  subject_claim: string
  email_claim: string
  auto_provision_role: "viewer" | "developer" | null
}

/** Roles the control plane will grant automatically on a first sign-in. */
export const autoProvisionRoles = [
  { value: "none", label: "Admit people individually" },
  { value: "viewer", label: "Viewer" },
  { value: "developer", label: "Developer" },
] as const

const claimPattern = /^[A-Za-z0-9_:./-]{1,100}$/

function httpsUrl(raw: string, label: string) {
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    throw new Error(`Enter the ${label} as a full https:// URL.`)
  }
  if (url.protocol !== "https:" || !url.hostname) throw new Error(`The ${label} must use https://.`)
  if (url.username || url.password) throw new Error(`The ${label} must not contain credentials.`)
  return raw
}

export function parseIdentityProvider(form: FormData): IdentityProviderInput {
  const read = (key: string) => String(form.get(key) ?? "").trim()
  const issuer = httpsUrl(read("issuer"), "issuer")
  const audience = read("audience")
  if (!audience || audience.length > 500) throw new Error("Enter the audience the provider issues tokens for.")
  const jwksUri = read("jwksUri")
  const subjectClaim = read("subjectClaim") || "sub"
  const emailClaim = read("emailClaim") || "email"
  for (const [claim, label] of [[subjectClaim, "subject"], [emailClaim, "email"]] as const) {
    if (!claimPattern.test(claim)) throw new Error(`Enter the ${label} claim name, such as ${label === "subject" ? "sub" : "email"}.`)
  }
  const role = read("autoProvisionRole") || "none"
  if (!autoProvisionRoles.some((option) => option.value === role)) throw new Error("Choose a supported automatic role.")
  return {
    issuer: issuer.replace(/\/+$/, ""),
    audience,
    jwks_uri: jwksUri ? httpsUrl(jwksUri, "JWKS URI") : null,
    subject_claim: subjectClaim,
    email_claim: emailClaim,
    auto_provision_role: role === "none" ? null : role as "viewer" | "developer",
  }
}
