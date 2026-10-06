import "server-only"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { summarizeAccountUsage } from "@/lib/fabric/account-usage"
import { getConsoleContext, getFabricAccessToken } from "@/lib/fabric/session"
import type {
  AccountUsage,
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
  const token = await getFabricAccessToken(context.account.id)
  return controlPlaneRequest<T>(path, { ...init, token })
}

export async function listDeployments() {
  const context = await getConsoleContext()
  return accountRequest<Deployment[]>(
    `/v1/accounts/${context.account.id}/deployments`
  )
}

export async function getDeployment(deploymentId: string) {
  const context = await getConsoleContext()
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
  return accountRequest<Placement[]>(
    `/v1/accounts/${context.account.id}/deployments/${deploymentId}/placements`
  )
}

export async function getDeploymentStatuses(deploymentId: string) {
  const context = await getConsoleContext()
  return accountRequest<DeploymentStatus[]>(
    `/v1/accounts/${context.account.id}/deployments/${deploymentId}/status`
  )
}

export async function getAccountUsage() {
  const context = await getConsoleContext()
  try {
    return await accountRequest<AccountUsage>(
      `/v1/accounts/${context.account.id}/deployments/usage`
    )
  } catch (error) {
    // Older control planes either lack this route (404) or match "usage" to
    // their UUID deployment route (422). Authentication and outages must surface.
    if (!(error instanceof FabricApiError) || ![404, 422].includes(error.status)) {
      throw error
    }
  }

  const deployments = await listDeployments()
  const usages: DeploymentUsage[] = []
  // Keep compatibility reads bounded even for accounts with many deployments.
  // A failed deployment read rejects the whole result instead of undercounting.
  for (let index = 0; index < deployments.length; index += 4) {
    usages.push(...await Promise.all(
      deployments.slice(index, index + 4).map((deployment) => getDeploymentUsage(deployment.id))
    ))
  }
  return summarizeAccountUsage(usages)
}

export async function getDeploymentUsage(deploymentId: string) {
  const context = await getConsoleContext()
  return accountRequest<DeploymentUsage>(
    `/v1/accounts/${context.account.id}/deployments/${deploymentId}/usage`
  )
}

export async function listStamps() {
  const context = await getConsoleContext()
  return accountRequest<Stamp[]>(`/v1/accounts/${context.account.id}/stamps`)
}

export async function listMembers() {
  const context = await getConsoleContext()
  return accountRequest<Membership[]>(
    `/v1/accounts/${context.account.id}/members`
  )
}

export async function listServicePrincipals() {
  const context = await getConsoleContext()
  return accountRequest<ServicePrincipal[]>(
    `/v1/accounts/${context.account.id}/service-principals`
  )
}

export async function listApiKeys() {
  const context = await getConsoleContext()
  return accountRequest<ApiKey[]>(
    `/v1/accounts/${context.account.id}/api-keys`
  )
}

export async function getOidcProvider() {
  const context = await getConsoleContext()
  try {
    return await accountRequest<OidcProvider>(
      `/v1/accounts/${context.account.id}/oidc-provider`
    )
  } catch (error) {
    if (error instanceof FabricApiError && error.status === 404) return null
    throw error
  }
}
