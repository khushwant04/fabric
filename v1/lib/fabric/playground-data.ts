import "server-only"

import { getDeploymentStatuses, listDeployments, listPlacements, listStamps } from "@/lib/fabric/data"
import { configuredInferenceUrl, edgeInferenceUrl, gatewayDisplayUrl, readyPlacements, resolveInferenceUrl, selectorGateway, stampInferenceUrls, type InferenceConfiguration, type PlaygroundOption } from "@/lib/fabric/playground"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import type { Stamp } from "@/lib/fabric/types"

export function inferenceConfiguration(): InferenceConfiguration {
  return {
    configuredUrl: process.env.FABRIC_INFERENCE_URL,
    edgeUrl: process.env.FABRIC_EDGE_INFERENCE_URL,
    allowedOrigins: process.env.FABRIC_INFERENCE_ALLOWED_ORIGINS,
    allowedDomains: process.env.FABRIC_INFERENCE_ALLOWED_DOMAINS,
    production: process.env.NODE_ENV === "production",
  }
}

/** The edge gateway's public base URL for the Overview and Stamps cards, or null. */
export function edgeGatewayUrl() {
  try {
    const edge = edgeInferenceUrl(inferenceConfiguration())
    return edge ? gatewayDisplayUrl(edge) : null
  } catch {
    return null
  }
}

/** Whether FABRIC_INFERENCE_URL is the edge too, so the card can say the console uses it. */
export function consoleUsesEdge() {
  try {
    const configuration = inferenceConfiguration()
    const edge = edgeInferenceUrl(configuration)
    const configured = configuredInferenceUrl(configuration)
    return !!edge && (!configured || configured.origin === edge.origin)
  } catch {
    return false
  }
}

export function stampNames(stamps: Stamp[] | undefined) {
  return new Map((stamps ?? []).map((stamp) => [stamp.id, stamp.name]))
}

export async function getPlaygroundOptions() {
  const context = await getConsoleContext()
  if (!hasScope(context, "deployments:read")) return { options: [], notice: "Your account role does not allow deployment access." }
  const canReadStamps = hasScope(context, "stamps:read")
  const [deployments, stamps] = await Promise.all([
    listDeployments(),
    canReadStamps ? listStamps().catch(() => undefined) : Promise.resolve(undefined),
  ])
  const configuration = inferenceConfiguration()
  const stampUrls = stampInferenceUrls(stamps)
  const candidates = await Promise.all(deployments.map(async (deployment) => {
    const [placements, statuses] = await Promise.all([listPlacements(deployment.id), getDeploymentStatuses(deployment.id)])
    try {
      const ready = readyPlacements(deployment, placements, statuses)
      const endpoint = resolveInferenceUrl(ready, configuration, stampUrls)
      return { deployment, endpoint, stampId: ready[0]?.stamp_id ?? null }
    } catch { return null }
  }))
  const available = candidates.filter((candidate) => candidate !== null)
  const options: PlaygroundOption[] = available.map(({ deployment }) => ({
    id: deployment.id, label: `${deployment.model_alias} · ${deployment.name}`,
    model: "exact", deploymentId: deployment.id, modelAlias: deployment.model_alias,
  }))
  // Auto is resolved on one gateway: the edge, or the stamp gateway serving most of the
  // account's ready deployments. Each exact model above goes to its own stamp's gateway.
  const selector = selectorGateway(available)
  if (selector && !deployments.some((deployment) => deployment.model_alias === "auto")) {
    const stamp = selector.gateways > 1 && selector.anchor.stampId ? stampNames(stamps).get(selector.anchor.stampId) : null
    options.unshift({
      id: "virtual-auto",
      label: selector.gateways > 1 ? `Auto · model selection on ${stamp ?? "one stamp"}` : "Auto · gateway model selection",
      model: "auto", deploymentId: selector.anchor.deployment.id, modelAlias: "auto",
    })
  }
  return {
    options,
    notice: options.length ? null : "No ready model is available through an approved inference gateway. Configure gateway access and wait for a deployment to become ready.",
  }
}
