import "server-only"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { getDeploymentStatuses, listDeployments, listPlacements, listStamps } from "@/lib/fabric/data"
import { deploymentState, servesTraffic } from "@/lib/fabric/resource-state"
import type { DeploymentOption, PolicyVersion, RoutingPolicy, RoutingPolicySummary } from "@/lib/fabric/routing-policy"
import { getConsoleContext, getFabricAccessToken, hasScope } from "@/lib/fabric/session"

async function read<T>(suffix: string): Promise<T> {
  const context = await getConsoleContext()
  const token = await getFabricAccessToken(context.account.id)
  return controlPlaneRequest<T>(`/v1/accounts/${context.account.id}/routing-policies${suffix}`, { token })
}

export async function listRoutingPolicies() {
  return read<RoutingPolicySummary[]>("")
}

export async function getRoutingPolicy(policyId: string) {
  if (!/^[0-9a-fA-F-]{36}$/.test(policyId)) return null
  try {
    return await read<RoutingPolicy>(`/${policyId}`)
  } catch (error) {
    if (error instanceof FabricApiError && error.status === 404) return null
    throw error
  }
}

export async function listRoutingPolicyVersions(policyId: string) {
  return read<PolicyVersion[]>(`/${policyId}/versions`)
}

/** The account's deployments as the routing editor shows them, with observed readiness. */
export async function routingDeploymentOptions(): Promise<DeploymentOption[]> {
  const context = await getConsoleContext()
  const [deployments, stamps] = await Promise.all([
    listDeployments(),
    hasScope(context, "stamps:read") ? listStamps().catch(() => undefined) : Promise.resolve(undefined),
  ])
  return Promise.all(deployments.map(async (item) => {
    let ready = false
    try {
      const [statuses, placements] = await Promise.all([getDeploymentStatuses(item.id), listPlacements(item.id)])
      // "Ready" here means the edge can route to it: at least one ready replica.
      ready = servesTraffic(deploymentState(item, statuses, placements, stamps))
    } catch {
      ready = false
    }
    const runtime = item.desired_spec.runtime
    return {
      id: item.id,
      name: item.name,
      alias: item.model_alias,
      ready,
      tools: runtime.enable_auto_tool_choice !== false && Boolean(runtime.tool_call_parser || runtime.enable_auto_tool_choice),
    }
  }))
}
