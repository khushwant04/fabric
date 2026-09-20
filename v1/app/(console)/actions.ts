"use server"

import { randomUUID } from "node:crypto"
import { revalidatePath } from "next/cache"
import { redirect } from "next/navigation"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import {
  getConsoleContext,
  getFabricAccessToken,
  requireScope,
} from "@/lib/fabric/session"
import type {
  ApiKey,
  Deployment,
  Membership,
  OidcProvider,
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
  if (context.demo) throw new Error("Mutations are disabled in preview mode")
  const token = await getFabricAccessToken(context.account.id)
  return { context, token }
}

export async function createDeployment(formData: FormData) {
  const { context, token } = await mutationContext("deployments:write")
  const name = value(formData, "name")
  const modelAlias = value(formData, "modelAlias")
  const release = value(formData, "release")
  const replicas = Number(value(formData, "replicas"))
  const gpuCount = Number(value(formData, "gpuCount"))
  const gpuClass = value(formData, "gpuClass")
  const region = value(formData, "region")

  if (!/^[a-z0-9]([a-z0-9-]{0,198}[a-z0-9])?$/.test(name)) {
    throw new Error("Deployment names must use lowercase letters, numbers, and hyphens")
  }
  if (!modelAlias || !release || !Number.isInteger(replicas) || replicas < 1 || replicas > 32) {
    throw new Error("Complete all required deployment fields")
  }

  const deployment = await controlPlaneRequest<Deployment>(
    `/v1/accounts/${context.account.id}/deployments`,
    {
      method: "POST",
      token,
      headers: { "Idempotency-Key": randomUUID() },
      body: JSON.stringify({
        name,
        model_alias: modelAlias,
        spec: {
          runtime: {
            release,
            kernel_mode: value(formData, "kernelMode") || "auto",
            strategy: value(formData, "strategy") || "least_in_flight",
          },
          replicas,
          resources: { gpu_count: gpuCount, gpu_class: gpuClass },
          limits_policy_ref: value(formData, "limitsPolicy") || null,
        },
      }),
    }
  )

  let placementFailed = false
  try {
    await controlPlaneRequest(
      `/v1/accounts/${context.account.id}/deployments/${deployment.id}/placements`,
      {
        method: "POST",
        token,
        body: JSON.stringify(region ? { region } : {}),
      }
    )
  } catch (error) {
    if (!(error instanceof FabricApiError)) throw error
    placementFailed = true
  }
  revalidatePath("/deployments")
  redirect(
    `/deployments/${deployment.id}${placementFailed ? "?placement=failed" : ""}`
  )
}

export async function deleteDeployment(
  deploymentId: string,
  formData: FormData
) {
  void formData
  const { context, token } = await mutationContext("deployments:write")
  await controlPlaneRequest(
    `/v1/accounts/${context.account.id}/deployments/${deploymentId}`,
    { method: "DELETE", token }
  )
  revalidatePath("/deployments")
  redirect("/deployments")
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

export async function saveOidcProvider(
  _previous: ActionState,
  formData: FormData
): Promise<ActionState> {
  try {
    const { context, token } = await mutationContext("members:write")
    await controlPlaneRequest<OidcProvider>(
      `/v1/accounts/${context.account.id}/oidc-provider`,
      {
        method: "PUT",
        token,
        body: JSON.stringify({
          issuer: value(formData, "issuer"),
          audience: value(formData, "audience"),
          jwks_uri: value(formData, "jwksUri") || null,
          subject_claim: value(formData, "subjectClaim") || "sub",
          email_claim: value(formData, "emailClaim") || "email",
          auto_provision_role:
            value(formData, "autoProvisionRole") === "none"
              ? null
              : value(formData, "autoProvisionRole") || null,
        }),
      }
    )
    revalidatePath("/access/identity-provider")
    return { ok: true }
  } catch (error) {
    return { error: actionError(error) }
  }
}
