import "server-only"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import {
  demoApiKeys,
  demoDeployments,
  demoOidcProvider,
  demoPrincipals,
  demoStamps,
  demoStatuses,
  demoUsage,
} from "@/lib/fabric/demo-data"
import { getConsoleContext, getFabricAccessToken } from "@/lib/fabric/session"
import type {
  ApiKey,
  Deployment,
  DeploymentStatus,
  DeploymentUsage,
  Membership,
  OidcProvider,
  Placement,
  ServicePrincipal,
  Stamp,
} from "@/lib/fabric/types"

async function accountRequest<T>(path: string, init?: RequestInit) {
  const context = await getConsoleContext()
  if (context.demo) throw new Error("Demo requests must use fixture data")
  const token = await getFabricAccessToken(context.account.id)
  return controlPlaneRequest<T>(path, { ...init, token })
}

export async function listDeployments() {
  const context = await getConsoleContext()
  if (context.demo) return demoDeployments
  return accountRequest<Deployment[]>(
    `/v1/accounts/${context.account.id}/deployments`
  )
}

export async function getDeployment(deploymentId: string) {
  const context = await getConsoleContext()
  if (context.demo) {
    return demoDeployments.find((item) => item.id === deploymentId) ?? null
  }
  try {
    return await accountRequest<Deployment>(
      `/v1/accounts/${context.account.id}/deployments/${deploymentId}`
    )
  } catch (error) {
    if (error instanceof FabricApiError && error.status === 404) return null
    throw error
  }
}

export async function listPlacements(deploymentId: string) {
  const context = await getConsoleContext()
  if (context.demo) {
    return demoStatuses
      .filter((item) => item.deployment_id === deploymentId)
      .map<Placement>((item, index) => ({
        id: `placement_${index}`,
        account_id: context.account.id,
        deployment_id: item.deployment_id,
        stamp_id: item.stamp_id,
        desired_generation: item.observed_generation ?? 1,
        observed_generation: item.observed_generation,
        status: item.phase,
      }))
  }
  return accountRequest<Placement[]>(
    `/v1/accounts/${context.account.id}/deployments/${deploymentId}/placements`
  )
}

export async function getDeploymentStatuses(deploymentId: string) {
  const context = await getConsoleContext()
  if (context.demo) {
    return demoStatuses.filter((item) => item.deployment_id === deploymentId)
  }
  return accountRequest<DeploymentStatus[]>(
    `/v1/accounts/${context.account.id}/deployments/${deploymentId}/status`
  )
}

export async function getDeploymentUsage(deploymentId: string) {
  const context = await getConsoleContext()
  if (context.demo) {
    return (
      demoUsage[deploymentId] ?? {
        deployment_id: deploymentId,
        events: 0,
        input_tokens: 0,
        output_tokens: 0,
        first_occurred_at: null,
        last_occurred_at: null,
        stamps: [],
      }
    )
  }
  return accountRequest<DeploymentUsage>(
    `/v1/accounts/${context.account.id}/deployments/${deploymentId}/usage`
  )
}

export async function listStamps() {
  const context = await getConsoleContext()
  if (context.demo) return demoStamps
  return accountRequest<Stamp[]>(`/v1/accounts/${context.account.id}/stamps`)
}

export async function listMembers() {
  const context = await getConsoleContext()
  if (context.demo) return context.me.memberships
  return accountRequest<Membership[]>(
    `/v1/accounts/${context.account.id}/members`
  )
}

export async function listServicePrincipals() {
  const context = await getConsoleContext()
  if (context.demo) return demoPrincipals
  return accountRequest<ServicePrincipal[]>(
    `/v1/accounts/${context.account.id}/service-principals`
  )
}

export async function listApiKeys() {
  const context = await getConsoleContext()
  if (context.demo) return demoApiKeys
  return accountRequest<ApiKey[]>(
    `/v1/accounts/${context.account.id}/api-keys`
  )
}

export async function getOidcProvider() {
  const context = await getConsoleContext()
  if (context.demo) return demoOidcProvider
  try {
    return await accountRequest<OidcProvider>(
      `/v1/accounts/${context.account.id}/oidc-provider`
    )
  } catch (error) {
    if (error instanceof FabricApiError && error.status === 404) return null
    throw error
  }
}
