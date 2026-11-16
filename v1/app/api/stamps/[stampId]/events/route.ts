import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { getAuthSession, getConsoleContext, getFabricAccessToken, hasScope } from "@/lib/fabric/session"
import { isStampId } from "@/lib/fabric/stamp-activity"
import type { StampEvents } from "@/lib/fabric/types"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

function failure(status: number, message: string) {
  return Response.json({ error: { message } }, { status, headers: { "Cache-Control": "no-store" } })
}

// A stamp's activity log for the stamp page, polled every few seconds while it is open. A
// route handler rather than a Server Function, for the same reason as the deployment hosts
// poll: Server Functions run one at a time and a poll would queue every other action behind
// it. The control token stays on the server; the account is the session's, never the URL's.
export async function GET(_request: Request, ctx: RouteContext<"/api/stamps/[stampId]/events">) {
  const { stampId } = await ctx.params
  if (!isStampId(stampId)) return failure(404, "Stamp not found.")
  if (!(await getAuthSession())) return failure(401, "Sign in to view stamp activity.")
  try {
    const context = await getConsoleContext()
    if (!hasScope(context, "stamps:read")) return failure(403, "Viewing stamp activity needs the stamps:read scope.")
    const token = await getFabricAccessToken(context.account.id)
    const events = await controlPlaneRequest<StampEvents>(
      `/v1/accounts/${encodeURIComponent(context.account.id)}/stamps/${stampId}/events`,
      { token }
    )
    return Response.json(events, { headers: { "Cache-Control": "no-store" } })
  } catch (error) {
    if (error instanceof FabricApiError) return failure(error.status, error.message)
    throw error
  }
}
