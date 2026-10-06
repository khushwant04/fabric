import { readyPlacements } from "./playground"
import type { Deployment, DeploymentStatus, Placement, Stamp } from "./types"

export function publicGatewayBase(raw: unknown): string | null {
  if (typeof raw !== "string") return null
  try {
    const url = new URL(raw)
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.search || url.hash ||
        !["/", "/v1", "/v1/"].includes(url.pathname)) return null
    return `${url.origin}/v1`
  } catch { return null }
}

export function deploymentEndpoints(deployment: Deployment, placements: Placement[], statuses: DeploymentStatus[], stamps?: Stamp[]) {
  return [...new Set(readyPlacements(deployment, placements, statuses, stamps)
    .map((status) => publicGatewayBase(status.endpoint)).filter((url): url is string => url !== null))]
}
