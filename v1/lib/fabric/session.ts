import "server-only"

import { cache } from "react"
import { cookies } from "next/headers"
import { redirect } from "next/navigation"

import { auth0, isAuth0Configured } from "@/lib/auth0"
import { controlPlaneRequest } from "@/lib/fabric/client"
import { demoConsoleContext } from "@/lib/fabric/demo-data"
import type {
  Account,
  ConsoleContext,
  Me,
  TokenResponse,
} from "@/lib/fabric/types"

const ACCOUNT_COOKIE = "fabric_account_id"
const TOKEN_EXPIRY_SKEW_MS = 30_000
const tokenCache = new Map<
  string,
  { accessToken: string; expiresAt: number }
>()

export const isDemoMode =
  process.env.FABRIC_DEMO_MODE === "true" &&
  process.env.NODE_ENV !== "production"

function trimTokenCache() {
  const now = Date.now()
  for (const [key, entry] of tokenCache) {
    if (entry.expiresAt <= now) tokenCache.delete(key)
  }
  if (tokenCache.size > 500) tokenCache.delete(tokenCache.keys().next().value ?? "")
}

export const getAuthSession = cache(async () => {
  if (!auth0) return null
  return auth0.getSession()
})

export const getAuth0AccessToken = cache(async () => {
  if (!auth0) return null
  const { token } = await auth0.getAccessToken()
  return token
})

export const getMe = cache(async (): Promise<Me | null> => {
  if (isDemoMode) return demoConsoleContext.me
  const token = await getAuth0AccessToken()
  if (!token) return null
  return controlPlaneRequest<Me>("/v1/me", { token })
})

export async function getSelectedAccountId() {
  return (await cookies()).get(ACCOUNT_COOKIE)?.value ?? null
}

export async function setSelectedAccount(accountId: string) {
  const cookieStore = await cookies()
  cookieStore.set(ACCOUNT_COOKIE, accountId, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  })
}

export async function clearSelectedAccount() {
  (await cookies()).delete(ACCOUNT_COOKIE)
}

export const getFabricAccessToken = cache(async (accountId: string) => {
  if (isDemoMode) return "demo-token"
  if (!auth0) throw new Error("Auth0 is not configured")

  const session = await getAuthSession()
  if (!session) redirect("/auth/login?returnTo=/onboarding")

  const subject = String(session.user.sub ?? "")
  const cacheKey = `${subject}:${accountId}`
  const cached = tokenCache.get(cacheKey)
  if (cached && cached.expiresAt - TOKEN_EXPIRY_SKEW_MS > Date.now()) {
    return cached.accessToken
  }

  const assertion = await getAuth0AccessToken()
  if (!assertion) redirect("/auth/login?returnTo=/onboarding")

  const exchanged = await controlPlaneRequest<TokenResponse>("/v1/token", {
    method: "POST",
    body: JSON.stringify({
      grant_type: "auth0_token",
      audience: "fabric-control",
      assertion,
      account_id: accountId,
    }),
  })

  trimTokenCache()
  tokenCache.set(cacheKey, {
    accessToken: exchanged.access_token,
    expiresAt: Date.now() + exchanged.expires_in * 1_000,
  })
  return exchanged.access_token
})

export const getConsoleContext = cache(async (): Promise<ConsoleContext> => {
  if (isDemoMode) return demoConsoleContext
  if (!isAuth0Configured || !auth0) redirect("/?configuration=required")

  const session = await getAuthSession()
  if (!session) redirect("/auth/login?returnTo=/onboarding")

  const me = await getMe()
  if (!me) redirect("/auth/login?returnTo=/onboarding")

  const accountId = await getSelectedAccountId()
  const membership = me.memberships.find(
    (item) => item.account_id === accountId && item.status === "active"
  )
  if (!accountId || !membership) redirect("/onboarding")

  const token = await getFabricAccessToken(accountId)
  const [account, identity] = await Promise.all([
    controlPlaneRequest<Account>(`/v1/accounts/${accountId}`, { token }),
    controlPlaneRequest<ConsoleContext["identity"]>("/v1/self", { token }),
  ])

  return { account, identity, me, demo: false }
})

export function hasScope(context: ConsoleContext, scope: string) {
  return context.identity.scopes.includes(scope)
}

export function requireScope(context: ConsoleContext, scope: string) {
  if (!hasScope(context, scope)) {
    throw new Error(`Missing required scope: ${scope}`)
  }
}
