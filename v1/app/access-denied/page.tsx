import { redirect } from "next/navigation"
import { ShieldCheckIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { getAuthSession, getMe } from "@/lib/fabric/session"
import { getSingleTenantAccountId } from "@/lib/fabric/single-tenant"

export const dynamic = "force-dynamic"

export default async function AccessDeniedPage() {
  const session = await getAuthSession()
  if (!session) redirect("/auth/login?returnTo=/onboarding")
  const accountId = getSingleTenantAccountId()
  if (!accountId) redirect("/onboarding")
  const me = await getMe()
  if (me?.memberships.some((item) => item.account_id === accountId && item.status === "active")) redirect("/dashboard")

  return <main className="flex min-h-svh items-center justify-center bg-muted/30 p-6"><Card className="w-full max-w-md"><CardHeader><ShieldCheckIcon className="mb-3 size-6" /><CardTitle>Workspace access required</CardTitle><CardDescription>Your sign-in is valid, but this installation requires an active membership in its configured workspace. Ask your administrator to grant access.</CardDescription></CardHeader><CardContent><Button variant="outline" render={<a href="/auth/logout" />}>Sign out</Button></CardContent></Card></main>
}
