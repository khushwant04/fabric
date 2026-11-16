import "server-only"

import { getDeploymentStatuses, listDeployments, listPlacements, listStamps } from "@/lib/fabric/data"
import { AgentRunsError, gatewayBase, mergeRunLists, parseRunList, parseRunTrace, runGatewayBases, runsResponseError, runsUrl, type RunSummary, type RunTrace } from "@/lib/fabric/agent-runs"
import { readyPlacements, resolveInferenceUrl, selectorGateway, stampInferenceUrls } from "@/lib/fabric/playground"
import { inferenceConfiguration, stampNames } from "@/lib/fabric/playground-data"
import { getConsoleContext, getFabricInferenceToken, hasScope } from "@/lib/fabric/session"
import type { Deployment } from "@/lib/fabric/types"

export type AgentModelOption = {
  id: string
  label: string
  description: string
  model: "exact" | "auto" | "cascade"
  deploymentId: string
  modelAlias: string
}

export type VectorStoreOption = { id: string; name: string; files: number }

const GATEWAY_TIMEOUT_MS = 15_000

async function readyDeployments(canReadStamps: boolean) {
  const [deployments, stamps] = await Promise.all([
    listDeployments(),
    // Stamp inference URLs back up a status without an endpoint; optional.
    canReadStamps ? listStamps().catch(() => undefined) : Promise.resolve(undefined),
  ])
  const ready = await Promise.all(deployments.map(async (deployment) => {
    try {
      const [placements, statuses] = await Promise.all([listPlacements(deployment.id), getDeploymentStatuses(deployment.id)])
      return { deployment, ready: readyPlacements(deployment, placements, statuses) }
    } catch { return { deployment, ready: [] } }
  }))
  return { deployments: ready, stamps, stampUrls: stampInferenceUrls(stamps) }
}

async function gatewayContext() {
  const context = await getConsoleContext()
  if (!hasScope(context, "deployments:read")) throw new AgentRunsError(403, "Your account role does not allow deployment access.", "unauthorized")
  const { deployments, stampUrls } = await readyDeployments(hasScope(context, "stamps:read"))
  const bases = runGatewayBases(deployments.map((item) => item.ready), inferenceConfiguration(), stampUrls)
  return { context, deployments, bases }
}

async function gatewayFetch(url: string, token: string, init: RequestInit = {}) {
  const response = await fetch(url, {
    ...init, redirect: "manual", cache: "no-store", signal: AbortSignal.timeout(GATEWAY_TIMEOUT_MS),
    headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
  })
  let body: unknown = null
  if (response.status !== 204) {
    const text = await response.text()
    if (text.length > 5_000_000) throw new AgentRunsError(502, "The inference gateway returned an oversized response.")
    try { body = text ? JSON.parse(text) : null } catch { body = null }
  }
  return { status: response.status, body }
}

function noGateway() {
  return new AgentRunsError(503, "No approved inference gateway is available. Configure FABRIC_EDGE_INFERENCE_URL or FABRIC_INFERENCE_URL, or wait for a deployment to become ready behind an approved gateway.", "unavailable")
}

export type RunListResult = { runs: RunSummary[]; error: string | null; unsupported: boolean; partial: boolean }

export async function listAgentRuns(): Promise<RunListResult> {
  try {
    const { context, bases } = await gatewayContext()
    if (!bases.length) throw noGateway()
    const token = await getFabricInferenceToken(context.account.id)
    const results = await Promise.allSettled(bases.map(async (base) => {
      const { status, body } = await gatewayFetch(runsUrl(base), token)
      if (status !== 200) throw runsResponseError(status, body)
      return parseRunList(body)
    }))
    const lists = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : [])
    const failures = results.flatMap((result) => result.status === "rejected" ? [result.reason] : [])
    if (!lists.length) {
      const first = failures[0]
      if (first instanceof AgentRunsError) return { runs: [], error: first.message, unsupported: failures.every((item) => item instanceof AgentRunsError && item.kind === "unsupported"), partial: false }
      return { runs: [], error: "Run traces could not be loaded from the inference gateway.", unsupported: false, partial: false }
    }
    return { runs: mergeRunLists(lists), error: failures.length ? "Some inference gateways could not be checked." : null, unsupported: false, partial: failures.length > 0 }
  } catch (error) {
    if (error instanceof AgentRunsError) return { runs: [], error: error.message, unsupported: error.kind === "unsupported", partial: false }
    // Navigation errors (redirect to sign-in) must propagate.
    if (error && typeof error === "object" && "digest" in error) throw error
    return { runs: [], error: "Run traces could not be loaded from the inference gateway.", unsupported: false, partial: false }
  }
}

/** Asks each candidate gateway in turn; a run lives on exactly one stamp. */
export async function getAgentRun(runId: string): Promise<RunTrace | null> {
  const { context, bases } = await gatewayContext()
  if (!bases.length) throw noGateway()
  const token = await getFabricInferenceToken(context.account.id)
  let lastError: AgentRunsError | null = null
  for (const base of bases) {
    const { status, body } = await gatewayFetch(runsUrl(base, runId), token)
    if (status === 200) return parseRunTrace(body)
    const error = runsResponseError(status, body)
    if (error.kind !== "not_found") lastError = error
  }
  if (lastError) throw lastError
  return null
}

export async function deleteAgentRunOnGateway(runId: string) {
  const { context, bases } = await gatewayContext()
  if (!bases.length) throw noGateway()
  const token = await getFabricInferenceToken(context.account.id)
  for (const base of bases) {
    const { status, body } = await gatewayFetch(runsUrl(base, runId), token, { method: "DELETE" })
    if (status === 204 || status === 200) return
    const error = runsResponseError(status, body)
    if (error.kind !== "not_found") throw error
  }
  throw new AgentRunsError(404, "This run is not available. It may have expired or been deleted.", "not_found")
}

function vectorStores(body: unknown): VectorStoreOption[] {
  const data = body && typeof body === "object" && Array.isArray((body as { data?: unknown }).data) ? (body as { data: unknown[] }).data : []
  return data.slice(0, 100).flatMap((item) => {
    const store = item && typeof item === "object" ? item as Record<string, unknown> : null
    if (!store || typeof store.id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(store.id)) return []
    const counts = store.file_counts && typeof store.file_counts === "object" ? store.file_counts as Record<string, unknown> : {}
    const name = typeof store.name === "string" && store.name.trim() ? store.name.replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 120) : store.id
    return [{ id: store.id, name, files: typeof counts.completed === "number" ? counts.completed : typeof counts.total === "number" ? counts.total : 0 }]
  })
}

export async function getAgentPlaygroundOptions() {
  const context = await getConsoleContext()
  if (!hasScope(context, "deployments:read")) return { options: [], notice: "Your account role does not allow deployment access.", vectorStores: [], vectorStoreNotice: null }
  const { deployments, stamps, stampUrls } = await readyDeployments(hasScope(context, "stamps:read"))
  const configuration = inferenceConfiguration()
  const available = deployments.flatMap(({ deployment, ready }) => {
    try { return [{ deployment, endpoint: resolveInferenceUrl(ready, configuration, stampUrls), stampId: ready[0]?.stamp_id ?? null }] } catch { return [] }
  })
  const options: AgentModelOption[] = available.map(({ deployment }) => ({
    id: deployment.id, label: deployment.model_alias, description: deployment.name, model: "exact", deploymentId: deployment.id, modelAlias: deployment.model_alias,
  }))
  // auto and cascade choose among the account's deployments on one gateway: the edge, or the
  // stamp gateway serving most of them. Exact models go to their own stamp's gateway.
  const selector = selectorGateway(available)
  const owns = (alias: string) => deployments.some(({ deployment }: { deployment: Deployment }) => deployment.model_alias === alias)
  const where = selector && selector.gateways > 1 && selector.anchor.stampId ? ` on ${stampNames(stamps).get(selector.anchor.stampId) ?? "one stamp"}` : ""
  if (selector && !owns("cascade")) options.unshift({ id: "virtual-cascade", label: "cascade", description: `Smallest deployment first, escalate on failed checks${where}`, model: "cascade", deploymentId: selector.anchor.deployment.id, modelAlias: "cascade" })
  if (selector && !owns("auto")) options.unshift({ id: "virtual-auto", label: "auto", description: `Gateway model selection${where}`, model: "auto", deploymentId: selector.anchor.deployment.id, modelAlias: "auto" })

  let stores: VectorStoreOption[] = []
  let vectorStoreNotice: string | null = null
  if (available.length) {
    try {
      const token = await getFabricInferenceToken(context.account.id)
      const { status, body } = await gatewayFetch(`${gatewayBase(selector?.endpoint ?? available[0].endpoint)}/vector_stores?limit=100`, token)
      if (status === 200) stores = vectorStores(body)
      else if (status === 400 || status === 404 || status === 405 || status === 503) vectorStoreNotice = "File search is not available on this gateway."
      else vectorStoreNotice = "Vector stores could not be listed."
    } catch (error) {
      if (error && typeof error === "object" && "digest" in error) throw error
      vectorStoreNotice = "Vector stores could not be listed."
    }
  }
  return {
    options,
    notice: options.length ? null : "No ready model is available through an approved inference gateway. Configure gateway access and wait for a deployment to become ready.",
    vectorStores: stores,
    vectorStoreNotice,
  }
}
