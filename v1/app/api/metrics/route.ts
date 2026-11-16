import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { parseWindow, type AccountMetrics } from "@/lib/fabric/serving-metrics"
import { getAuthSession, getConsoleContext, getFabricAccessToken, hasScope } from "@/lib/fabric/session"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

function failure(status: number, message: string) {
  return Response.json({ error: { message } }, { status, headers: { "Cache-Control": "no-store" } })
}

// Request rate and throughput across the account's deployments, for the overview's
// sparklines (docs/serving-metrics.md).
export async function GET(request: Request) {
  const window = parseWindow(new URL(request.url).searchParams.get("window"))
  if (!(await getAuthSession())) return failure(401, "Sign in to view metrics.")
  try {
    const context = await getConsoleContext()
    if (!hasScope(context, "deployments:read")) return failure(403, "Viewing metrics needs the deployments:read scope.")
    const token = await getFabricAccessToken(context.account.id)
    const metrics = await controlPlaneRequest<AccountMetrics>(
      `/v1/accounts/${encodeURIComponent(context.account.id)}/metrics?window=${window}`,
      { token }
    )
    return Response.json(metrics, { headers: { "Cache-Control": "no-store" } })
  } catch (error) {
    if (error instanceof FabricApiError) return failure(error.status, error.message)
    throw error
  }
}
