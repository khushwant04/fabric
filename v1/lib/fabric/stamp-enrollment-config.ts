import "server-only"

import { httpsControlPlaneUrl, type StampEnrollmentConfig } from "./stamp-enrollment"

export function getStampEnrollmentConfig(): StampEnrollmentConfig {
  return {
    controlPlaneUrl: httpsControlPlaneUrl(process.env.FABRIC_CONTROL_PLANE_PUBLIC_URL || "") ||
      httpsControlPlaneUrl(process.env.FABRIC_CONTROL_PLANE_URL || "") || "",
    jwtIssuer: process.env.FABRIC_CONTROL_PLANE_JWT_ISSUER || "",
  }
}

// The access token comes from the authenticated, scoped control-plane exchange.
// Only its public issuer is returned to the browser; the access token stays server-side.
export function controlPlaneTokenIssuer(token: string): string {
  try {
    const claims: unknown = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8"))
    if (claims && typeof claims === "object" && "iss" in claims && typeof claims.iss === "string") return claims.iss
  } catch { /* Configured issuer remains available when the token format changes. */ }
  return getStampEnrollmentConfig().jwtIssuer
}
