import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { isDeploymentId } from "@/lib/fabric/host-status"
import { parseWindow, type DeploymentMetrics } from "@/lib/fabric/serving-metrics"
import { getAuthSession, getConsoleContext, getFabricAccessToken, hasScope } from "@/lib/fabric/session"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

function failure(status: number, message: string) {
  return Response.json({ error: { message } }, { status, headers: { "Cache-Control": "no-store" } })
}

// Serving metrics for the deployment detail page's Metrics tab, polled while the tab is
// visible (docs/serving-metrics.md). A route handler rather than a Server Function, like the
// hosts route: those run one at a time and a poll would queue every other action behind it.
export async function GET(request: Request, ctx: RouteContext<"/api/deployments/[deploymentId]/metrics">) {
  const { deploymentId } = await ctx.params
  if (!isDeploymentId(deploymentId)) return failure(404, "Deployment not found.")
  const window = parseWindow(new URL(request.url).searchParams.get("window"))
  if (!(await getAuthSession())) return failure(401, "Sign in to view deployment metrics.")
  try {
    const context = await getConsoleContext()
    if (!hasScope(context, "deployments:read")) return failure(403, "Viewing metrics needs the deployments:read scope.")
    const token = await getFabricAccessToken(context.account.id)
    const metrics = await controlPlaneRequest<DeploymentMetrics>(
      `/v1/accounts/${encodeURIComponent(context.account.id)}/deployments/${deploymentId}/metrics?window=${window}`,
      { token }
    )
    return Response.json(metrics, { headers: { "Cache-Control": "no-store" } })
  } catch (error) {
    if (error instanceof FabricApiError) return failure(error.status, error.message)
    throw error
  }
}
