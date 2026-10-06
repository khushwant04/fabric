"use server"

import { randomUUID } from "node:crypto"
import { revalidatePath } from "next/cache"
import { redirect } from "next/navigation"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { parseNewDeployment, parsePlacementStamp } from "@/lib/fabric/new-deployment"
import {
  getConsoleContext,
  getFabricAccessToken,
  requireScope,
} from "@/lib/fabric/session"
import type {
  ApiKey,
  Deployment,
  Membership,
  ServicePrincipal,
} from "@/lib/fabric/types"

export type ActionState = {
  ok?: boolean
  error?: string
  secret?: string
  enrollmentToken?: string
  expiresAt?: string
}

function value(formData: FormData, key: string) {
  return String(formData.get(key) ?? "").trim()
}

function actionError(error: unknown) {
  if (error instanceof FabricApiError) return error.message
  return error instanceof Error ? error.message : "The operation could not be completed"
}

async function mutationContext(scope: string) {
  const context = await getConsoleContext()
  requireScope(context, scope)
  const token = await getFabricAccessToken(context.account.id)
  return { context, token }
}

export async function createDeployment(_previous: ActionState, formData: FormData): Promise<ActionState> {
  const { context, token } = await mutationContext("deployments:write")
  let deployment: Deployment
  let stampId: string | null
  try {
    const input = parseNewDeployment(formData)
    stampId = input.stampId
    deployment = await controlPlaneRequest<Deployment>(
    `/v1/accounts/${context.account.id}/deployments`,
    {
      method: "POST",
      token,
      headers: { "Idempotency-Key": randomUUID() },
      body: JSON.stringify({
        name: input.name,
        model_alias: input.modelAlias,
        spec: {
          runtime: {
            release: input.modelRef,
            kernel_mode: "standard",
            strategy: "least_in_flight",
            ...(input.maxModelLen ? { max_model_len: input.maxModelLen } : {}),
            ...(input.maxNumSeqs ? { max_num_seqs: input.maxNumSeqs } : {}),
            ...(input.execution ? { execution: input.execution } : {}),
          },
          replicas: input.replicas,
          resources: { gpu_count: input.gpuCount, gpu_class: input.gpuClass },
        },
      }),
    }
    )
  } catch (error) {
    return { error: actionError(error) }
  }

  let placementFailed = false
  try {
    await controlPlaneRequest(
      `/v1/accounts/${context.account.id}/deployments/${deployment.id}/placements`,
      {
        method: "POST",
        token,
        body: JSON.stringify(stampId ? { stamp_id: stampId } : {}),
      }
    )
  } catch {
    placementFailed = true
  }
  revalidatePath("/deployments")
  redirect(
    `/deployments/${deployment.id}${placementFailed ? "?placement=failed" : ""}`
  )
}

export async function assignDeployment(
  deploymentId: string,
  _previous: ActionState,
  formData: FormData
): Promise<ActionState> {
  const { context, token } = await mutationContext("deployments:write")
  try {
    const stampId = parsePlacementStamp(formData)
    const base = `/v1/accounts/${context.account.id}/deployments/${encodeURIComponent(deploymentId)}`
    const deployment = await controlPlaneRequest<Deployment>(base, { token })
    if (deployment.account_id !== context.account.id || ["terminating", "deleted"].includes(deployment.status)) {
      return { error: "This deployment cannot be assigned." }
    }
    await controlPlaneRequest(`${base}/placements`, {
      method: "POST",
      token,
      body: JSON.stringify(stampId ? { stamp_id: stampId } : {}),
    })
    revalidatePath(`/deployments/${deployment.id}`)
    revalidatePath("/deployments")
    return { ok: true }
  } catch (error) {
    return { error: actionError(error) }
  }
}

export async function createEnrollmentToken(
  _previous: ActionState,
  formData: FormData
): Promise<ActionState> {
  try {
    const { context, token } = await mutationContext("stamps:write")
    const result = await controlPlaneRequest<{
      enrollment_token: string
      expires_at: string
    }>(`/v1/accounts/${context.account.id}/stamp-enrollment-tokens`, {
      method: "POST",
      token,
      body: JSON.stringify({
        allowed_mode: value(formData, "mode") || "byoi",
        expires_in_minutes: Number(value(formData, "expiresInMinutes") || "60"),
      }),
    })
    return {
      ok: true,
      enrollmentToken: result.enrollment_token,
      expiresAt: result.expires_at,
    }
  } catch (error) {
    return { error: actionError(error) }
  }
}

export async function addMember(
  _previous: ActionState,
  formData: FormData
): Promise<ActionState> {
  try {
    const { context, token } = await mutationContext("members:write")
    await controlPlaneRequest<Membership>(
      `/v1/accounts/${context.account.id}/members`,
      {
        method: "POST",
        token,
        body: JSON.stringify({
          auth0_subject: value(formData, "subject"),
          email: value(formData, "email") || null,
          role: value(formData, "role"),
        }),
      }
    )
    revalidatePath("/access/members")
    return { ok: true }
  } catch (error) {
    return { error: actionError(error) }
  }
}

export async function createServicePrincipal(
  _previous: ActionState,
  formData: FormData
): Promise<ActionState> {
  try {
    const { context, token } = await mutationContext("api-keys:write")
    await controlPlaneRequest<ServicePrincipal>(
      `/v1/accounts/${context.account.id}/service-principals`,
      {
        method: "POST",
        token,
        body: JSON.stringify({ name: value(formData, "name") }),
      }
    )
    revalidatePath("/access/service-principals")
    return { ok: true }
  } catch (error) {
    return { error: actionError(error) }
  }
}

export async function createApiKey(
  _previous: ActionState,
  formData: FormData
): Promise<ActionState> {
  try {
    const { context, token } = await mutationContext("api-keys:write")
    const scopes = formData.getAll("scopes").map(String)
    const result = await controlPlaneRequest<{ api_key: ApiKey; secret: string }>(
      `/v1/accounts/${context.account.id}/api-keys`,
      {
        method: "POST",
        token,
        body: JSON.stringify({
          name: value(formData, "name"),
          scopes,
          expires_in_days: value(formData, "expiresInDays")
            ? Number(value(formData, "expiresInDays"))
            : null,
          service_principal_id:
            value(formData, "servicePrincipalId") === "human"
              ? null
              : value(formData, "servicePrincipalId") || null,
        }),
      }
    )
    revalidatePath("/access/api-keys")
    return { ok: true, secret: result.secret }
  } catch (error) {
    return { error: actionError(error) }
  }
}
