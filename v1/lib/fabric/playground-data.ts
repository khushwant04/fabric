import "server-only"

import { getDeploymentStatuses, listDeployments, listPlacements } from "@/lib/fabric/data"
import { readyPlacements, resolveInferenceUrl, type InferenceConfiguration, type PlaygroundOption } from "@/lib/fabric/playground"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"

export function inferenceConfiguration(): InferenceConfiguration {
  return {
    configuredUrl: process.env.FABRIC_INFERENCE_URL,
    allowedOrigins: process.env.FABRIC_INFERENCE_ALLOWED_ORIGINS,
    production: process.env.NODE_ENV === "production",
  }
}

export async function getPlaygroundOptions() {
  const context = await getConsoleContext()
  if (!hasScope(context, "deployments:read")) return { options: [], notice: "Your account role does not allow deployment access." }
  const deployments = await listDeployments()
  const configuration = inferenceConfiguration()
  const candidates = await Promise.all(deployments.map(async (deployment) => {
    const [placements, statuses] = await Promise.all([listPlacements(deployment.id), getDeploymentStatuses(deployment.id)])
    try {
      const ready = readyPlacements(deployment, placements, statuses)
      const endpoint = resolveInferenceUrl(ready, configuration)
      return { deployment, endpoint }
    } catch { return null }
  }))
  const available = candidates.filter((candidate) => candidate !== null)
  const options: PlaygroundOption[] = available.map(({ deployment }) => ({
    id: deployment.id, label: `${deployment.model_alias} · ${deployment.name}`,
    model: "exact", deploymentId: deployment.id, modelAlias: deployment.model_alias,
  }))
  // Auto is stamp-local. Only offer a common gateway when all eligible deployments
  // share it, so the selector is not presented as global fleet routing.
  if (available.length && new Set(available.map((item) => item.endpoint)).size === 1 &&
      !deployments.some((deployment) => deployment.model_alias === "auto")) {
    options.unshift({ id: "virtual-auto", label: "Auto · gateway model selection", model: "auto", deploymentId: available[0].deployment.id, modelAlias: "auto" })
  }
  return {
    options,
    notice: options.length ? null : "No ready model is available through an approved inference gateway. Configure gateway access and wait for a deployment to become ready.",
  }
}
