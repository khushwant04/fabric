"use server"

import { refresh } from "next/cache"
import { unstable_rethrow } from "next/navigation"

import { getConsoleContext } from "@/lib/fabric/session"

export async function refreshConsoleView() {
  try {
    // Account selection, membership, and token exchange remain on the server.
    // Each refreshed page makes its own account-scoped, no-store API reads.
    await getConsoleContext()
    refresh()
    return { ok: true }
  } catch (error) {
    unstable_rethrow(error)
    return { error: "Refresh failed. Check your connection and try again." }
  }
}
