import { Building2Icon, CheckIcon, KeyRoundIcon, ShieldCheckIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge } from "@/components/console/status-badge"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { getConsoleContext } from "@/lib/fabric/session"
import { formatDate, shortId } from "@/lib/format"

export default async function SettingsPage() {
  const context = await getConsoleContext()
  return <PageContainer><PageHeader title="Account settings" description="Review tenant information, entitlements, and the effective console identity." />
    <div className="grid gap-5 xl:grid-cols-[1fr_340px]"><div className="space-y-5"><Card><CardHeader><CardTitle>Tenant information</CardTitle><CardDescription>The account boundary attached to every control-plane request.</CardDescription></CardHeader><CardContent><dl className="divide-y rounded-lg border text-sm">{[{ label: "Account name", value: context.account.name }, { label: "Slug", value: context.account.slug }, { label: "Account ID", value: shortId(context.account.id) }, { label: "Created", value: formatDate(context.account.created_at) }].map((item) => <div className="grid gap-1 px-4 py-3 sm:grid-cols-[180px_1fr]" key={item.label}><dt className="text-muted-foreground">{item.label}</dt><dd className={item.label === "Account ID" ? "font-mono" : "font-medium"}>{item.value}</dd></div>)}</dl></CardContent></Card>
      <Card><CardHeader><CardTitle>Effective permissions</CardTitle><CardDescription>UI actions are gated by these token scopes and checked again by the API.</CardDescription></CardHeader><CardContent><div className="flex flex-wrap gap-2">{context.identity.scopes.map((scope) => <Badge key={scope} variant="secondary" className="font-mono text-[10px]"><CheckIcon />{scope}</Badge>)}</div></CardContent></Card></div>
      <div className="space-y-5"><Card><CardHeader><CardTitle className="flex items-center gap-2"><Building2Icon className="size-4" /> Account state</CardTitle></CardHeader><CardContent className="gap-4"><div className="flex items-center justify-between"><span className="text-sm text-muted-foreground">Status</span><StatusBadge status={context.account.status} /></div><Separator /><div className="flex items-center justify-between"><span className="text-sm text-muted-foreground">Managed capacity</span><Badge variant={context.account.managed_capacity_enabled ? "default" : "outline"}>{context.account.managed_capacity_enabled ? "Enabled" : "Disabled"}</Badge></div></CardContent></Card>
      <Card><CardHeader><CardTitle className="flex items-center gap-2"><ShieldCheckIcon className="size-4" /> Session identity</CardTitle></CardHeader><CardContent><dl className="space-y-4 text-sm"><div><dt className="text-xs text-muted-foreground">Principal type</dt><dd className="mt-1 capitalize">{context.identity.principal_type.replaceAll("_", " ")}</dd></div><div><dt className="text-xs text-muted-foreground">Audience</dt><dd className="mt-1 font-mono text-xs">{context.identity.audience}</dd></div><div><dt className="text-xs text-muted-foreground">Token handling</dt><dd className="mt-1 flex items-center gap-2"><KeyRoundIcon className="size-3.5" /> Server-only exchange</dd></div></dl></CardContent></Card></div></div>
  </PageContainer>
}
