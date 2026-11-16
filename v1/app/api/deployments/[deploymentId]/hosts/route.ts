import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { isDeploymentId } from "@/lib/fabric/host-status"
import { getAuthSession, getConsoleContext, getFabricAccessToken, hasScope } from "@/lib/fabric/session"
import type { DeploymentHosts } from "@/lib/fabric/types"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

function failure(status: number, message: string) {
  return Response.json({ error: { message } }, { status, headers: { "Cache-Control": "no-store" } })
}

// Model-host status and log tails for the deployment detail page, polled every few seconds
// while a host starts (docs/model-host-logs.md). A route handler rather than a Server
// Function: those are dispatched one at a time, and a poll this frequent would queue
// every other action on the page behind it. The control token stays on the server.
export async function GET(_request: Request, ctx: RouteContext<"/api/deployments/[deploymentId]/hosts">) {
  const { deploymentId } = await ctx.params
  if (!isDeploymentId(deploymentId)) return failure(404, "Deployment not found.")
  if (!(await getAuthSession())) return failure(401, "Sign in to view deployment hosts.")
  try {
    const context = await getConsoleContext()
    if (!hasScope(context, "deployments:read")) return failure(403, "Viewing hosts needs the deployments:read scope.")
    const token = await getFabricAccessToken(context.account.id)
    const hosts = await controlPlaneRequest<DeploymentHosts>(
      `/v1/accounts/${encodeURIComponent(context.account.id)}/deployments/${deploymentId}/hosts`,
      { token }
    )
    return Response.json(hosts, { headers: { "Cache-Control": "no-store" } })
  } catch (error) {
    if (error instanceof FabricApiError) return failure(error.status, error.message)
    throw error
  }
}
