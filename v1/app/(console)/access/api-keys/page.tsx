import { KeyRoundIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Section } from "@/components/console/section"
import { TruncatedText } from "@/components/console/truncated-text"
import { CreateApiKeyDrawer } from "@/components/console/resource-dialogs"
import { StatusBadge } from "@/components/console/status-badge"
import { Badge } from "@/components/ui/badge"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listApiKeys, listServicePrincipals } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { auditActorLabel } from "@/lib/fabric/audit"
import { formatDate, formatDay } from "@/lib/format"

export default async function ApiKeysPage() {
  const [context, keys, principals] = await Promise.all([getConsoleContext(), listApiKeys(), listServicePrincipals()])
  const canWrite = hasScope(context, "api-keys:write")
  return <PageContainer><PageHeader title="API keys" description="Issue narrowly scoped credentials for automation and development workflows." actions={<CreateApiKeyDrawer principals={principals} disabled={!canWrite} />} />
    <Section title="Account credentials" description="Secrets are returned only at creation. Revoke keys that are no longer needed.">{keys.length ? <Table className="min-w-[880px]"><TableHeader><TableRow><TableHead>Key</TableHead><TableHead>Owner</TableHead><TableHead>Scopes</TableHead><TableHead>Status</TableHead><TableHead>Expires</TableHead><TableHead className="text-right">Created</TableHead></TableRow></TableHeader><TableBody>{keys.map((key) => <TableRow key={key.id}><TableCell className="h-14 font-medium"><TruncatedText text={key.name} className="max-w-56" /><TruncatedText text={`${key.key_prefix}••••`} className="max-w-56 font-mono text-[11px] font-normal text-muted-foreground" /></TableCell><TableCell className="whitespace-nowrap">{auditActorLabel(key.principal_type)}</TableCell><TableCell className="whitespace-normal"><div className="flex min-w-48 max-w-sm flex-wrap gap-1">{key.scopes.slice(0, 3).map((scope) => <Badge variant="secondary" key={scope} className="font-mono text-[10px]">{scope}</Badge>)}{key.scopes.length > 3 ? <Badge variant="outline">+{key.scopes.length - 3}</Badge> : null}</div></TableCell><TableCell><StatusBadge status={key.revoked_at ? "revoked" : "active"} /></TableCell><TableCell className="whitespace-nowrap text-muted-foreground tabular-nums">{key.expires_at ? <time dateTime={key.expires_at} title={formatDate(key.expires_at)}>{formatDay(key.expires_at)}</time> : "Never"}</TableCell><TableCell className="text-right whitespace-nowrap text-muted-foreground tabular-nums"><time dateTime={key.created_at} title={formatDate(key.created_at)}>{formatDay(key.created_at)}</time></TableCell></TableRow>)}</TableBody></Table> : <Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><KeyRoundIcon /></EmptyMedia><EmptyTitle>No API keys</EmptyTitle><EmptyDescription>Create a scoped credential for an operator or service principal.</EmptyDescription></EmptyHeader></Empty>}</Section>
  </PageContainer>
}
