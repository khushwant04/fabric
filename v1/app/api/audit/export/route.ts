import { controlPlaneResponse, FabricApiError } from "@/lib/fabric/client"
import { auditQuery, parseAuditSearch } from "@/lib/fabric/audit"
import { getAuthSession, getConsoleContext, getFabricAccessToken, hasScope } from "@/lib/fabric/session"

const NEXT_CURSOR_HEADER = "Fabric-Next-Cursor"
// The control plane returns at most 5,000 events per request; this bounds one download
// to 50,000 so a single click cannot hold an unbounded trail in memory.
const MAX_PAGES = 10

function failure(status: number, message: string) {
  return Response.json({ error: { message } }, { status })
}

// Downloads the account's audit trail as newline-delimited JSON. The control token stays
// on the server; the browser only ever receives the export itself.
export async function GET(request: Request) {
  // Each export is recorded in the trail, so a cross-site page must not be able to trigger one.
  if (request.headers.get("sec-fetch-site") === "cross-site") {
    return failure(403, "This request must come from the Fabric console.")
  }
  if (!(await getAuthSession())) return failure(401, "Sign in to export the audit log.")
  try {
    const context = await getConsoleContext()
    if (!hasScope(context, "audit:read")) return failure(403, "Exporting the audit log needs the audit:read scope.")
    const token = await getFabricAccessToken(context.account.id)
    const url = new URL(request.url)
    const search = parseAuditSearch(Object.fromEntries(url.searchParams))
    const path = `/v1/accounts/${encodeURIComponent(context.account.id)}/audit-events/export`

    const chunks: string[] = []
    // Continue from the caller's cursor, so a download truncated at the page limit can resume.
    let cursor: string | undefined = search.cursor
    let truncated = false
    for (let page = 0; page < MAX_PAGES; page++) {
      const response = await controlPlaneResponse(`${path}?${auditQuery({ ...search, cursor })}`, { token, headers: { Accept: "application/x-ndjson" } })
      chunks.push(await response.text())
      cursor = response.headers.get(NEXT_CURSOR_HEADER) ?? undefined
      if (!cursor) break
      truncated = page === MAX_PAGES - 1
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-")
    return new Response(chunks.join(""), {
      headers: {
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "Content-Disposition": `attachment; filename="fabric-audit-${stamp}.ndjson"`,
        "Cache-Control": "no-store",
        ...(truncated && cursor ? { [NEXT_CURSOR_HEADER]: cursor } : {}),
      },
    })
  } catch (error) {
    if (error instanceof FabricApiError) return failure(error.status, error.message)
    throw error
  }
}
