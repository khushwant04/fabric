import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { isDeploymentId as isUuid } from "@/lib/fabric/host-status"
import { parseWindow, type StampMetrics } from "@/lib/fabric/serving-metrics"
import { getAuthSession, getConsoleContext, getFabricAccessToken, hasScope } from "@/lib/fabric/session"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

function failure(status: number, message: string) {
  return Response.json({ error: { message } }, { status, headers: { "Cache-Control": "no-store" } })
}

// Per-GPU utilisation and memory for the stamp detail page, polled while visible
// (docs/serving-metrics.md). The control token stays on the server.
export async function GET(request: Request, ctx: RouteContext<"/api/stamps/[stampId]/metrics">) {
  const { stampId } = await ctx.params
  if (!isUuid(stampId)) return failure(404, "Stamp not found.")
  const window = parseWindow(new URL(request.url).searchParams.get("window"))
  if (!(await getAuthSession())) return failure(401, "Sign in to view stamp metrics.")
  try {
    const context = await getConsoleContext()
    if (!hasScope(context, "stamps:read")) return failure(403, "Viewing stamp metrics needs the stamps:read scope.")
    const token = await getFabricAccessToken(context.account.id)
    const metrics = await controlPlaneRequest<StampMetrics>(
      `/v1/accounts/${encodeURIComponent(context.account.id)}/stamps/${stampId}/metrics?window=${window}`,
      { token }
    )
    return Response.json(metrics, { headers: { "Cache-Control": "no-store" } })
  } catch (error) {
    if (error instanceof FabricApiError) return failure(error.status, error.message)
    throw error
  }
}
