import { BotIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Section } from "@/components/console/section"
import { TruncatedText } from "@/components/console/truncated-text"
import { CreatePrincipalDrawer } from "@/components/console/resource-dialogs"
import { StatusBadge } from "@/components/console/status-badge"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listServicePrincipals } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate, shortId } from "@/lib/format"

export default async function ServicePrincipalsPage() {
  const [context, principals] = await Promise.all([getConsoleContext(), listServicePrincipals()])
  const canWrite = hasScope(context, "api-keys:write")
  return <PageContainer><PageHeader title="Service principals" description="Create non-human identities for gateways, automation, and CI systems." actions={<CreatePrincipalDrawer disabled={!canWrite} />} />
    <Section title="Automation identities" description="Disabling a principal also revokes all API keys that it owns.">{principals.length ? <Table className="min-w-[600px]"><TableHeader><TableRow><TableHead>Name</TableHead><TableHead>Principal ID</TableHead><TableHead>Status</TableHead><TableHead className="text-right">Created</TableHead></TableRow></TableHeader><TableBody>{principals.map((principal) => <TableRow key={principal.id}><TableCell className="font-medium"><TruncatedText text={principal.name} className="max-w-72" /></TableCell><TableCell className="font-mono text-xs text-muted-foreground">{shortId(principal.id)}</TableCell><TableCell><StatusBadge status={principal.status} /></TableCell><TableCell className="text-right text-muted-foreground tabular-nums">{formatDate(principal.created_at)}</TableCell></TableRow>)}</TableBody></Table> : <Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><BotIcon /></EmptyMedia><EmptyTitle>No service principals</EmptyTitle><EmptyDescription>Create an automation identity before assigning it an API key.</EmptyDescription></EmptyHeader></Empty>}</Section>
  </PageContainer>
}
