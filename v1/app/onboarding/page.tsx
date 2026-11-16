import { redirect } from "next/navigation"
import { ArrowRightIcon, Building2Icon, PlusIcon } from "lucide-react"

import { createAccount, selectAccount } from "@/app/onboarding/actions"
import { IconTile, Section, SectionBody } from "@/components/console/section"
import { FabricLogo } from "@/components/fabric-mark"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { getAuthSession, getMe, getSelectedAccountId } from "@/lib/fabric/session"
import { accountLabel } from "@/lib/fabric/people"
import { getSingleTenantAccountId } from "@/lib/fabric/single-tenant"
import { shortId } from "@/lib/format"

export default async function OnboardingPage({ searchParams }: PageProps<"/onboarding">) {
  const query = await searchParams
  const session = await getAuthSession()
  if (!session) redirect("/auth/login?returnTo=/onboarding")
  const me = await getMe()
  if (!me) redirect("/")
  const selected = await getSelectedAccountId()
  if (getSingleTenantAccountId()) {
    if (me.memberships.some((item) => item.account_id === selected && item.status === "active")) redirect("/dashboard")
    redirect("/access-denied")
  }
  if (
    !query.switch &&
    selected &&
    me.memberships.some((item) => item.account_id === selected)
  )
    redirect("/dashboard")

  return (
    <main className="min-h-svh bg-muted/40 px-5 py-10 sm:px-10 sm:py-16">
      <div className="mx-auto flex max-w-2xl flex-col gap-8">
        <FabricLogo />
        <div><h1 className="text-[28px] leading-9 font-medium tracking-[-0.025em]">Choose your workspace</h1><p className="mt-1.5 text-[15px] text-muted-foreground">Select the account you want to operate, or create a new one.</p></div>
        <Tabs defaultValue={me.memberships.length ? "accounts" : "new"} className="gap-5">
          <TabsList><TabsTrigger value="accounts">Your accounts</TabsTrigger><TabsTrigger value="new">Create account</TabsTrigger></TabsList>
          <TabsContent value="accounts">
            <Section title="Available accounts" description="Your permissions are resolved again after you select an account.">
              {me.memberships.length ? <ul className="border-t border-border">{me.memberships.map((membership) => <li key={membership.account_id} className="border-b border-border last:border-b-0"><form action={selectAccount} className="flex items-center gap-4 px-6 py-4"><input type="hidden" name="accountId" value={membership.account_id} /><IconTile tone="neutral"><Building2Icon /></IconTile><div className="min-w-0 flex-1"><div className="flex min-w-0 items-center gap-2"><span className="truncate font-medium">{accountLabel(membership)}</span>{membership.account_id === selected ? <Badge className="shrink-0 bg-brand-tint text-brand-foreground">Current</Badge> : null}</div><div className="mt-0.5 truncate text-[13px] text-muted-foreground">{membership.account_slug ? <span className="font-mono">{membership.account_slug}</span> : <span className="font-mono">{shortId(membership.account_id)}</span>} · <span className="capitalize">{membership.role}</span> · <span className="capitalize">{membership.status}</span> membership</div></div><Button variant="outline" type="submit">Open <ArrowRightIcon /></Button></form></li>)}</ul> : <SectionBody><Alert><Building2Icon /><AlertTitle>No accounts yet</AlertTitle><AlertDescription>Create your first workspace to become its owner.</AlertDescription></Alert></SectionBody>}
            </Section>
          </TabsContent>
          <TabsContent value="new"><Section title="Create a Fabric account" description="Accounts isolate deployments, infrastructure, identities and credentials."><SectionBody><form action={createAccount} className="space-y-6"><FieldGroup><Field><FieldLabel htmlFor="name">Account name</FieldLabel><Input id="name" name="name" placeholder="Northstar Labs" required maxLength={200} /></Field><Field><FieldLabel htmlFor="slug">Account slug</FieldLabel><Input id="slug" name="slug" placeholder="northstar-labs" required pattern="[a-z0-9][a-z0-9-]{1,62}[a-z0-9]" /><FieldDescription>3–64 lowercase letters, numbers, and hyphens.</FieldDescription></Field></FieldGroup><div className="flex justify-end"><Button type="submit"><PlusIcon /> Create account</Button></div></form></SectionBody></Section></TabsContent>
        </Tabs>
      </div>
    </main>
  )
}
