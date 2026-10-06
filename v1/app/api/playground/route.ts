import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { inferenceConfiguration } from "@/lib/fabric/playground-data"
import { MAX_REQUEST_BYTES, PlaygroundError, readyPlacements, resolveInferenceUrl, routingDiagnostics, validatePlaygroundRequest } from "@/lib/fabric/playground"
import { getAuthSession, getConsoleContext, getFabricAccessToken, getFabricInferenceToken, hasScope } from "@/lib/fabric/session"
import type { Deployment, DeploymentStatus, Placement } from "@/lib/fabric/types"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

function failure(status: number, message: string) {
  return Response.json({ error: message }, { status, headers: { "Cache-Control": "no-store" } })
}

async function readBody(request: Request) {
  if (!request.headers.get("content-type")?.startsWith("application/json")) {
    throw new PlaygroundError(415, "Send a JSON request.")
  }
  if (Number(request.headers.get("content-length")) > MAX_REQUEST_BYTES) {
    throw new PlaygroundError(413, "Prompt request is too large.")
  }
  const reader = request.body?.getReader()
  if (!reader) throw new PlaygroundError(400, "Request body is missing.")
  const decoder = new TextDecoder()
  let bytes = 0
  let body = ""
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      bytes += chunk.value.byteLength
      if (bytes > MAX_REQUEST_BYTES) throw new PlaygroundError(413, "Prompt request is too large.")
      body += decoder.decode(chunk.value, { stream: true })
    }
    body += decoder.decode()
    try { return JSON.parse(body) as unknown } catch { throw new PlaygroundError(400, "Request is not valid JSON.") }
  } finally {
    if (bytes > MAX_REQUEST_BYTES) await reader.cancel()
    reader.releaseLock()
  }
}

export async function POST(request: Request) {
  // This cookie-authenticated endpoint accepts calls only from its console origin.
  const origin = request.headers.get("origin")
  const trustedOrigin = process.env.APP_BASE_URL ? new URL(process.env.APP_BASE_URL).origin : new URL(request.url).origin
  if (origin !== trustedOrigin || request.headers.get("sec-fetch-site") === "cross-site") {
    return failure(403, "This request must come from the Fabric console.")
  }
  if (!(await getAuthSession())) return failure(401, "Sign in to run inference.")

  const abort = new AbortController()
  const stop = () => abort.abort()
  request.signal.addEventListener("abort", stop, { once: true })
  if (request.signal.aborted) stop()
  const timer = setTimeout(stop, 180_000)
  const cleanup = () => {
    clearTimeout(timer)
    request.signal.removeEventListener("abort", stop)
  }
  try {
    const input = validatePlaygroundRequest(await readBody(request))
    const context = await getConsoleContext()
    if (!hasScope(context, "deployments:read")) throw new PlaygroundError(403, "Your account role does not allow deployment access.")
    // Route handlers are outside React's render memoization. Resolve the account
    // once and reuse its control token for every account-bound management read.
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
    if (!deployment || deployment.account_id !== context.account.id) throw new PlaygroundError(404, "Deployment is not available to this account.")
    const [placements, statuses] = await Promise.all([
      controlPlaneRequest<Placement[]>(`${deploymentPath}/placements`, { token: controlToken, signal: abort.signal }),
      controlPlaneRequest<DeploymentStatus[]>(`${deploymentPath}/status`, { token: controlToken, signal: abort.signal }),
    ])
    const url = resolveInferenceUrl(readyPlacements(deployment, placements, statuses), inferenceConfiguration())
    if (input.model === "auto") {
      const owned = await controlPlaneRequest<Deployment[]>(accountPath, { token: controlToken, signal: abort.signal })
      if (owned.some((item) => item.model_alias === "auto")) throw new PlaygroundError(400, "Choose the account's exact auto deployment.")
    }
    const token = await getFabricInferenceToken(context.account.id)
    const upstream = await fetch(url, {
      method: "POST", redirect: "manual", cache: "no-store", signal: abort.signal,
      headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify({
        model: input.model === "auto" ? "auto" : deployment.model_alias,
        messages: [{ role: "user", content: input.prompt }],
        max_tokens: input.maxTokens, temperature: input.temperature,
        stream: true, stream_options: { include_usage: true },
        routing: { ...(input.task ? { task: input.task } : {}), explain: true },
      }),
    })
    if (!upstream.ok || !upstream.body || !upstream.headers.get("content-type")?.includes("text/event-stream")) {
      await upstream.body?.cancel()
      const status = upstream.status === 429 ? 429 : 502
      throw new PlaygroundError(status, upstream.status === 429 ? "Inference capacity limit reached. Try again shortly." : `Inference gateway could not start a stream (${upstream.status}).`)
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
          controller.error(new Error("Inference stream interrupted."))
        }
      },
      async cancel() {
        stop()
        cleanup()
        try { await reader.cancel() } catch { /* Disconnect is already handled. */ }
        reader.releaseLock()
      },
    })
    return new Response(stream, {
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store, no-transform",
        "X-Accel-Buffering": "no", ...routingDiagnostics(upstream.headers),
      },
    })
  } catch (error) {
    cleanup()
    stop()
    if (error instanceof PlaygroundError) return failure(error.status, error.message)
    if (error instanceof FabricApiError) return failure(error.status === 403 ? 403 : 502, "Fabric could not authorize or resolve this deployment.")
    return failure(request.signal.aborted ? 499 : 502, "Inference could not be started. Check your session and gateway configuration.")
  }
}
