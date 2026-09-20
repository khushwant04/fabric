import { redirect } from "next/navigation"
import { Building2Icon, CheckIcon, PlusIcon } from "lucide-react"

import { createAccount, selectAccount } from "@/app/onboarding/actions"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { getAuthSession, getMe, getSelectedAccountId, isDemoMode } from "@/lib/fabric/session"
import { shortId } from "@/lib/format"

export default async function OnboardingPage({ searchParams }: PageProps<"/onboarding">) {
  const query = await searchParams
  if (isDemoMode) redirect("/dashboard")
  const session = await getAuthSession()
  if (!session) redirect("/auth/login?returnTo=/onboarding")
  const me = await getMe()
  if (!me) redirect("/")
  const selected = await getSelectedAccountId()
  if (
    !query.switch &&
    selected &&
    me.memberships.some((item) => item.account_id === selected)
  )
    redirect("/dashboard")

  return (
    <main className="min-h-svh bg-muted/30 p-5 sm:p-10">
      <div className="mx-auto flex max-w-3xl flex-col gap-7">
        <div className="flex items-center gap-2 text-sm font-semibold"><span className="flex size-7 items-center justify-center rounded-md border bg-background shadow-xs">F</span> Fabric</div>
        <div><h1 className="text-3xl font-semibold tracking-tight">Choose your workspace</h1><p className="mt-2 text-sm text-muted-foreground">Select the account you want to operate or create a new one.</p></div>
        <Tabs defaultValue={me.memberships.length ? "accounts" : "new"}>
          <TabsList variant="line"><TabsTrigger value="accounts">Your accounts</TabsTrigger><TabsTrigger value="new">Create account</TabsTrigger></TabsList>
          <TabsContent value="accounts" className="pt-5">
            <Card><CardHeader><CardTitle>Available accounts</CardTitle><CardDescription>Your permissions are resolved again after you select an account.</CardDescription></CardHeader><CardContent>
              {me.memberships.length ? <div className="divide-y rounded-lg border">{me.memberships.map((membership) => <form action={selectAccount} key={membership.account_id} className="flex items-center gap-4 p-4"><input type="hidden" name="accountId" value={membership.account_id} /><div className="flex size-9 items-center justify-center rounded-md border bg-muted"><Building2Icon className="size-4" /></div><div className="min-w-0 flex-1"><div className="font-medium">Account {shortId(membership.account_id)}</div><div className="mt-1 text-xs text-muted-foreground">{membership.status} membership</div></div><Badge variant="outline" className="capitalize">{membership.role}</Badge><Button variant="outline" type="submit">Open <CheckIcon /></Button></form>)}</div> : <Alert><Building2Icon /><AlertTitle>No accounts yet</AlertTitle><AlertDescription>Create your first workspace to become its owner.</AlertDescription></Alert>}
            </CardContent></Card>
          </TabsContent>
          <TabsContent value="new" className="pt-5"><Card><CardHeader><CardTitle>Create a Fabric account</CardTitle><CardDescription>Accounts isolate deployments, infrastructure, identities, and credentials.</CardDescription></CardHeader><CardContent><form action={createAccount} className="space-y-6"><FieldGroup><Field><FieldLabel htmlFor="name">Account name</FieldLabel><Input id="name" name="name" placeholder="Northstar Labs" required maxLength={200} /></Field><Field><FieldLabel htmlFor="slug">Account slug</FieldLabel><Input id="slug" name="slug" placeholder="northstar-labs" required pattern="[a-z0-9][a-z0-9-]{1,62}[a-z0-9]" /><FieldDescription>3–64 lowercase letters, numbers, and hyphens.</FieldDescription></Field></FieldGroup><div className="flex justify-end"><Button type="submit"><PlusIcon /> Create account</Button></div></form></CardContent></Card></TabsContent>
        </Tabs>
      </div>
    </main>
  )
}
