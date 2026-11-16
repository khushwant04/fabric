"use server"

import { revalidatePath } from "next/cache"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { parseIdentityProvider } from "@/lib/fabric/identity-provider"
import { getConsoleContext, getFabricAccessToken, requireScope } from "@/lib/fabric/session"
import type { OidcProvider } from "@/lib/fabric/types"

export type IdentityProviderActionState = { ok?: boolean; error?: string }

const PAGE = "/access/identity-provider"

function message(error: unknown, fallback: string) {
  if (error instanceof FabricApiError) {
    if (error.status === 403) return error.message || "You do not have permission to manage this account's identity provider."
    return error.message || fallback
  }
  return error instanceof Error ? error.message : fallback
}

/** Registers the account's provider, or replaces the one already registered (PUT is idempotent). */
export async function saveIdentityProvider(_previous: IdentityProviderActionState, formData: FormData): Promise<IdentityProviderActionState> {
  try {
    const context = await getConsoleContext()
    requireScope(context, "members:write")
    const input = parseIdentityProvider(formData)
    const token = await getFabricAccessToken(context.account.id)
    await controlPlaneRequest<OidcProvider>(`/v1/accounts/${context.account.id}/oidc-provider`, {
      method: "PUT",
      token,
      body: JSON.stringify(input),
    })
    revalidatePath(PAGE)
    return { ok: true }
  } catch (error) {
    return { error: message(error, "The identity provider could not be saved.") }
  }
}

export async function removeIdentityProvider(): Promise<IdentityProviderActionState> {
  try {
    const context = await getConsoleContext()
    requireScope(context, "members:write")
    const token = await getFabricAccessToken(context.account.id)
    await controlPlaneRequest(`/v1/accounts/${context.account.id}/oidc-provider`, { method: "DELETE", token })
    revalidatePath(PAGE)
    return { ok: true }
  } catch (error) {
    if (error instanceof FabricApiError && error.status === 404) {
      revalidatePath(PAGE)
      return { ok: true }
    }
    return { error: message(error, "The identity provider could not be removed.") }
  }
}
