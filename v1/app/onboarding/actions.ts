"use server"

import { redirect } from "next/navigation"

import { controlPlaneRequest } from "@/lib/fabric/client"
import {
  getAuth0AccessToken,
  getMe,
  setSelectedAccount,
} from "@/lib/fabric/session"
import type { Account } from "@/lib/fabric/types"

const ACCOUNT_ID_PATTERN = /^[a-zA-Z0-9_-]{3,128}$/

export async function selectAccount(formData: FormData) {
  const accountId = String(formData.get("accountId") ?? "")
  if (!ACCOUNT_ID_PATTERN.test(accountId)) throw new Error("Invalid account")

  const me = await getMe()
  const allowed = me?.memberships.some(
    (membership) =>
      membership.account_id === accountId && membership.status === "active"
  )
  if (!allowed) throw new Error("You do not have access to this account")

  await setSelectedAccount(accountId)
  redirect("/dashboard")
}

export async function createAccount(formData: FormData) {
  const name = String(formData.get("name") ?? "").trim()
  const slug = String(formData.get("slug") ?? "").trim().toLowerCase()
  if (name.length < 1 || name.length > 200) throw new Error("Invalid account name")
  if (!/^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/.test(slug)) {
    throw new Error("Use 3–64 lowercase letters, numbers, or hyphens")
  }

  const token = await getAuth0AccessToken()
  if (!token) redirect("/auth/login?returnTo=/onboarding")

  const account = await controlPlaneRequest<Account>("/v1/accounts", {
    method: "POST",
    token,
    body: JSON.stringify({ name, slug }),
  })
  await setSelectedAccount(account.id)
  redirect("/dashboard")
}
