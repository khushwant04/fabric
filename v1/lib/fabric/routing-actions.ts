"use server"

import { revalidatePath } from "next/cache"
import { redirect, unstable_rethrow } from "next/navigation"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import {
  aliasProblem,
  emptyDocument,
  normalizeDocument,
  type PolicyDocument,
  type PolicyVersion,
  type RoutingPolicy,
  type SimulatedRequest,
  type SimulateResult,
  type ValidationIssue,
} from "@/lib/fabric/routing-policy"
import { getConsoleContext, getFabricAccessToken, requireScope } from "@/lib/fabric/session"

export type RoutingActionResult<T> = { ok: true; value: T } | { ok: false; error: string; issues?: ValidationIssue[] }

const POLICY_ID = /^[0-9a-fA-F-]{36}$/

async function call<T>(scope: string, suffix: string, init: RequestInit = {}): Promise<T> {
  const context = await getConsoleContext()
  requireScope(context, scope)
  const token = await getFabricAccessToken(context.account.id)
  return controlPlaneRequest<T>(`/v1/accounts/${context.account.id}/routing-policies${suffix}`, { ...init, token })
}

function failure(error: unknown, fallback: string): { ok: false; error: string; issues?: ValidationIssue[] } {
  unstable_rethrow(error)
  if (error instanceof FabricApiError) {
    if (error.status === 403) return { ok: false, error: "You do not have permission to change routing policies." }
    if (error.status === 404) return { ok: false, error: "This routing policy is no longer available." }
    const details = error.details as { errors?: ValidationIssue[] } | undefined
    return { ok: false, error: error.message, issues: details?.errors }
  }
  if (error instanceof Error && error.message.startsWith("Missing required scope")) {
    return { ok: false, error: "You do not have permission to change routing policies." }
  }
  return { ok: false, error: fallback }
}

export async function saveRoutingDraft(policyId: string, draft: PolicyDocument): Promise<RoutingActionResult<RoutingPolicy>> {
  if (!POLICY_ID.test(policyId)) return { ok: false, error: "Choose a valid policy." }
  try {
    const value = await call<RoutingPolicy>("deployments:write", `/${policyId}/draft`, {
      method: "PUT",
      body: JSON.stringify({ draft: normalizeDocument(draft) }),
    })
    return { ok: true, value }
  } catch (error) {
    return failure(error, "The draft could not be saved. Try again.")
  }
}

export async function publishRoutingPolicy(policyId: string, draft: PolicyDocument, note: string): Promise<RoutingActionResult<RoutingPolicy>> {
  if (!POLICY_ID.test(policyId)) return { ok: false, error: "Choose a valid policy." }
  try {
    // Publish exactly what the person reviewed: save it as the draft first.
    await call<RoutingPolicy>("deployments:write", `/${policyId}/draft`, {
      method: "PUT",
      body: JSON.stringify({ draft: normalizeDocument(draft) }),
    })
    const value = await call<RoutingPolicy>("deployments:write", `/${policyId}/publish`, {
      method: "POST",
      body: JSON.stringify({ note: note.slice(0, 500) }),
    })
    revalidatePath("/routing")
    return { ok: true, value }
  } catch (error) {
    return failure(error, "Publishing failed. Nothing was changed.")
  }
}

export async function rollbackRoutingPolicy(policyId: string, version: number): Promise<RoutingActionResult<RoutingPolicy>> {
  if (!POLICY_ID.test(policyId) || !Number.isInteger(version) || version < 1) return { ok: false, error: "Choose a valid version." }
  try {
    const value = await call<RoutingPolicy>("deployments:write", `/${policyId}/rollback`, {
      method: "POST",
      body: JSON.stringify({ version }),
    })
    revalidatePath("/routing")
    return { ok: true, value }
  } catch (error) {
    return failure(error, "Rollback failed. Nothing was changed.")
  }
}

export async function listRoutingVersions(policyId: string): Promise<RoutingActionResult<PolicyVersion[]>> {
  if (!POLICY_ID.test(policyId)) return { ok: false, error: "Choose a valid policy." }
  try {
    return { ok: true, value: await call<PolicyVersion[]>("deployments:read", `/${policyId}/versions`) }
  } catch (error) {
    return failure(error, "Version history could not be loaded.")
  }
}

export async function simulateRoutingPolicy(
  policyId: string,
  source: "draft" | "live",
  request: SimulatedRequest,
  document?: PolicyDocument
): Promise<RoutingActionResult<SimulateResult>> {
  if (!POLICY_ID.test(policyId)) return { ok: false, error: "Choose a valid policy." }
  try {
    const value = await call<SimulateResult>("deployments:read", `/${policyId}/simulate`, {
      method: "POST",
      body: JSON.stringify({ source, request, ...(source === "draft" && document ? { document: normalizeDocument(document) } : {}) }),
    })
    return { ok: true, value }
  } catch (error) {
    return failure(error, "The test could not be run.")
  }
}

export async function validateRoutingDraft(policyId: string, draft: PolicyDocument): Promise<RoutingActionResult<{ valid: boolean; errors: ValidationIssue[]; warnings: ValidationIssue[] }>> {
  if (!POLICY_ID.test(policyId)) return { ok: false, error: "Choose a valid policy." }
  try {
    const value = await call<{ valid: boolean; errors: ValidationIssue[]; warnings: ValidationIssue[] }>(
      "deployments:read", `/${policyId}/validate`, { method: "POST", body: JSON.stringify(normalizeDocument(draft)) }
    )
    return { ok: true, value }
  } catch (error) {
    return failure(error, "Validation could not be run.")
  }
}

export type CreatePolicyState = { error?: string }

export async function createRoutingPolicy(_previous: CreatePolicyState, formData: FormData): Promise<CreatePolicyState> {
  const alias = String(formData.get("alias") ?? "").trim()
  const description = String(formData.get("description") ?? "").trim().slice(0, 500)
  const deploymentId = String(formData.get("deployment_id") ?? "")
  const problem = aliasProblem(alias, [])
  if (problem) return { error: problem }
  if (!POLICY_ID.test(deploymentId)) return { error: "Choose the deployment that serves requests by default." }
  let created: RoutingPolicy
  try {
    created = await call<RoutingPolicy>("deployments:write", "", {
      method: "POST",
      body: JSON.stringify({ alias, description, draft: emptyDocument(deploymentId) }),
    })
  } catch (error) {
    return { error: failure(error, "The policy could not be created.").error }
  }
  revalidatePath("/routing")
  redirect(`/routing/${created.id}`)
}

export async function deleteRoutingPolicy(policyId: string): Promise<RoutingActionResult<null>> {
  if (!POLICY_ID.test(policyId)) return { ok: false, error: "Choose a valid policy." }
  try {
    await call<void>("deployments:write", `/${policyId}`, { method: "DELETE" })
  } catch (error) {
    return failure(error, "The policy could not be deleted.")
  }
  revalidatePath("/routing")
  redirect("/routing")
}
