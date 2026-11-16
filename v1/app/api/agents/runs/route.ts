import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { AGENT_MAX_REQUEST_BYTES, AgentPlaygroundError, agentRunError, buildAgentRun, validateAgentRunRequest } from "@/lib/fabric/agent-playground"
import { inferenceConfiguration } from "@/lib/fabric/playground-data"
import { PlaygroundError, readyPlacements, resolveInferenceUrl } from "@/lib/fabric/playground"
import { getAuthSession, getConsoleContext, getFabricAccessToken, getFabricInferenceToken, hasScope } from "@/lib/fabric/session"
import type { Deployment, DeploymentStatus, Placement } from "@/lib/fabric/types"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

// Response headers the browser may see. Everything else from the gateway stays here.
const PASSTHROUGH_HEADERS = ["X-Fabric-Run-Id", "X-Fabric-Cascade-Path", "X-Fabric-Cascade-Escalations", "X-Fabric-Cascade-Reason"] as const

function failure(status: number, message: string, extra: Record<string, string> = {}) {
  return Response.json({ error: message, ...extra }, { status, headers: { "Cache-Control": "no-store" } })
}

async function readBody(request: Request) {
  if (!request.headers.get("content-type")?.startsWith("application/json")) throw new AgentPlaygroundError(415, "Send a JSON request.")
  if (Number(request.headers.get("content-length")) > AGENT_MAX_REQUEST_BYTES) throw new AgentPlaygroundError(413, "The run request is too large.")
  const reader = request.body?.getReader()
  if (!reader) throw new AgentPlaygroundError(400, "Request body is missing.")
  const decoder = new TextDecoder()
  let bytes = 0
  let body = ""
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      bytes += chunk.value.byteLength
      if (bytes > AGENT_MAX_REQUEST_BYTES) throw new AgentPlaygroundError(413, "The run request is too large.")
      body += decoder.decode(chunk.value, { stream: true })
    }
    body += decoder.decode()
    try { return JSON.parse(body) as unknown } catch { throw new AgentPlaygroundError(400, "Request is not valid JSON.") }
  } finally {
    if (bytes > AGENT_MAX_REQUEST_BYTES) await reader.cancel()
    reader.releaseLock()
  }
}

function agentRunsUrl(completionUrl: string) {
  const url = new URL(completionUrl)
  url.pathname = url.pathname.replace(/\/chat\/completions\/?$/, "/agent/runs")
  return url.toString()
}

async function readError(response: Response) {
  const text = (await response.text()).slice(0, 20_000)
  try { return JSON.parse(text) as unknown } catch { return null }
}

export async function POST(request: Request) {
  // This cookie-authenticated endpoint accepts calls only from its console origin.
  const origin = request.headers.get("origin")
  const trustedOrigin = process.env.APP_BASE_URL ? new URL(process.env.APP_BASE_URL).origin : new URL(request.url).origin
  if (origin !== trustedOrigin || request.headers.get("sec-fetch-site") === "cross-site") {
    return failure(403, "This request must come from the Fabric console.")
  }
  if (!(await getAuthSession())) return failure(401, "Sign in to run agents.")

  // Stop aborts the browser request; that aborts the gateway request, which cancels the run.
  const abort = new AbortController()
  const stop = () => abort.abort()
  request.signal.addEventListener("abort", stop, { once: true })
  if (request.signal.aborted) stop()
  const timer = setTimeout(stop, 600_000)
  const cleanup = () => {
    clearTimeout(timer)
    request.signal.removeEventListener("abort", stop)
  }
  try {
    const input = validateAgentRunRequest(await readBody(request))
    const context = await getConsoleContext()
    if (!hasScope(context, "deployments:read")) throw new AgentPlaygroundError(403, "Your account role does not allow deployment access.")
    const controlToken = await getFabricAccessToken(context.account.id)
    const accountPath = `/v1/accounts/${encodeURIComponent(context.account.id)}/deployments`
    const deploymentPath = `${accountPath}/${encodeURIComponent(input.deploymentId)}`
    let deployment: Deployment | null
    try {
      deployment = await controlPlaneRequest<Deployment>(deploymentPath, { token: controlToken, signal: abort.signal })
    } catch (error) {
      if (!(error instanceof FabricApiError) || error.status !== 404) throw error
      deployment = null
    }
    if (!deployment || deployment.account_id !== context.account.id) throw new AgentPlaygroundError(404, "Deployment is not available to this account.")
    const [placements, statuses] = await Promise.all([
      controlPlaneRequest<Placement[]>(`${deploymentPath}/placements`, { token: controlToken, signal: abort.signal }),
      controlPlaneRequest<DeploymentStatus[]>(`${deploymentPath}/status`, { token: controlToken, signal: abort.signal }),
    ])
    const url = agentRunsUrl(resolveInferenceUrl(readyPlacements(deployment, placements, statuses), inferenceConfiguration()))
    if (input.model !== "exact") {
      // A real deployment aliased auto or cascade takes precedence on the gateway; make the
      // caller pick it explicitly rather than presenting it as the virtual option.
      const owned = await controlPlaneRequest<Deployment[]>(accountPath, { token: controlToken, signal: abort.signal })
      if (owned.some((item) => item.model_alias === input.model)) throw new AgentPlaygroundError(400, `Choose the account's exact ${input.model} deployment.`)
    }
    const { headers, body } = buildAgentRun(input, deployment.model_alias)
    const token = await getFabricInferenceToken(context.account.id)
    const upstream = await fetch(url, {
      method: "POST", redirect: "manual", cache: "no-store", signal: abort.signal,
      headers: { ...headers, "Authorization": `Bearer ${token}`, "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify(body),
    })
    if (!upstream.ok || !upstream.body || !upstream.headers.get("content-type")?.includes("text/event-stream")) {
      throw agentRunError(upstream.ok ? 502 : upstream.status, upstream.ok ? null : await readError(upstream))
    }
    const reader = upstream.body.getReader()
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const chunk = await reader.read()
          if (chunk.done) { cleanup(); reader.releaseLock(); controller.close() }
          else controller.enqueue(chunk.value)
        } catch {
          cleanup()
          reader.releaseLock()
          controller.error(new Error("Agent stream interrupted."))
        }
      },
      async cancel() {
        stop()
        cleanup()
        try { await reader.cancel() } catch { /* Disconnect is already handled. */ }
        reader.releaseLock()
      },
    })
    const passthrough: Record<string, string> = {}
    for (const name of PASSTHROUGH_HEADERS) {
      const value = upstream.headers.get(name)
      if (value && /^[a-zA-Z0-9_.:>/ -]{1,512}$/.test(value)) passthrough[name] = value
    }
    return new Response(stream, {
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no", ...passthrough },
    })
  } catch (error) {
    cleanup()
    stop()
    if (error instanceof AgentPlaygroundError) return failure(error.status, error.message, { ...(error.code ? { code: error.code } : {}), ...(error.tool ? { tool: error.tool } : {}) })
    if (error instanceof PlaygroundError) return failure(error.status, error.message)
    if (error instanceof FabricApiError) return failure(error.status === 403 ? 403 : 502, "Fabric could not authorize or resolve this deployment.")
    return failure(request.signal.aborted ? 499 : 502, "The agent run could not be started. Check your session and gateway configuration.")
  }
}
