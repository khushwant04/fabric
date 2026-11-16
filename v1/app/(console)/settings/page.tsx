import { Building2Icon, CheckIcon, KeyRoundIcon, ShieldCheckIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Field, FieldList, Section, SectionBody } from "@/components/console/section"
import { StatusBadge } from "@/components/console/status-badge"
import { Badge } from "@/components/ui/badge"
import { getConsoleContext } from "@/lib/fabric/session"
import { formatDate, shortId } from "@/lib/format"

export default async function SettingsPage() {
  const context = await getConsoleContext()
  return <PageContainer><PageHeader title="Account settings" description="Review tenant information, entitlements, and the effective console identity." />
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_340px]"><div className="flex min-w-0 flex-col gap-4"><Section title="Tenant information" description="The account boundary attached to every control-plane request."><FieldList>{[{ label: "Account name", value: context.account.name }, { label: "Slug", value: context.account.slug }, { label: "Account ID", value: shortId(context.account.id) }, { label: "Created", value: formatDate(context.account.created_at) }].map((item) => <Field key={item.label} label={item.label} mono={item.label === "Account ID"}>{item.value}</Field>)}</FieldList></Section>
      <Section title="Effective permissions" description="UI actions are gated by these token scopes and checked again by the API." action={<span className="text-xs text-muted-foreground tabular-nums">{context.identity.scopes.length} scopes</span>}><SectionBody><div className="flex flex-wrap gap-1.5">{context.identity.scopes.map((scope) => <Badge key={scope} variant="outline" className="font-mono text-[11px] font-normal"><CheckIcon className="text-status-good" />{scope}</Badge>)}</div></SectionBody></Section></div>
      <div className="flex min-w-0 flex-col gap-4"><Section title={<><Building2Icon className="size-3.5" />Account state</>}><FieldList><div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3"><dt className="text-muted-foreground">Status</dt><dd><StatusBadge status={context.account.status} /></dd></div><div className="flex items-center justify-between gap-2 px-4 py-3"><dt className="text-muted-foreground">Managed capacity</dt><dd><Badge variant={context.account.managed_capacity_enabled ? "default" : "outline"}>{context.account.managed_capacity_enabled ? "Enabled" : "Disabled"}</Badge></dd></div></FieldList></Section>
      <Section title={<><ShieldCheckIcon className="size-3.5" />Session identity</>}><FieldList>{[{ label: "Principal type", value: <span className="capitalize">{context.identity.principal_type.replaceAll("_", " ")}</span> }, { label: "Audience", value: <span className="font-mono text-xs break-all">{context.identity.audience}</span> }, { label: "Token handling", value: <span className="flex items-center gap-2"><KeyRoundIcon className="size-3.5 text-muted-foreground" />Server-only exchange</span> }].map((item) => <div key={item.label} className="flex flex-col gap-1 border-b border-border px-4 py-3 last:border-b-0"><dt className="text-xs text-muted-foreground">{item.label}</dt><dd>{item.value}</dd></div>)}</FieldList></Section></div></div>
  </PageContainer>
}
