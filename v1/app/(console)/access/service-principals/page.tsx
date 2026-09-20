import { BotIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { CreatePrincipalDialog } from "@/components/console/resource-dialogs"
import { StatusBadge } from "@/components/console/status-badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listServicePrincipals } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate, shortId } from "@/lib/format"

export default async function ServicePrincipalsPage() {
  const [context, principals] = await Promise.all([getConsoleContext(), listServicePrincipals()])
  const canWrite = hasScope(context, "api-keys:write") && !context.demo
  return <PageContainer><PageHeader title="Service principals" description="Create non-human identities for gateways, automation, and CI systems." actions={<CreatePrincipalDialog disabled={!canWrite} />} />
    <Card><CardHeader><CardTitle>Automation identities</CardTitle><CardDescription>Disabling a principal also revokes all API keys that it owns.</CardDescription></CardHeader>{principals.length ? <CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Name</TableHead><TableHead>Principal ID</TableHead><TableHead>Status</TableHead><TableHead className="pr-6 text-right">Created</TableHead></TableRow></TableHeader><TableBody>{principals.map((principal) => <TableRow key={principal.id}><TableCell className="pl-6 font-medium">{principal.name}</TableCell><TableCell className="font-mono text-xs text-muted-foreground">{shortId(principal.id)}</TableCell><TableCell><StatusBadge status={principal.status} /></TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatDate(principal.created_at)}</TableCell></TableRow>)}</TableBody></Table></CardContent> : <Empty className="min-h-72 border-0"><EmptyHeader><EmptyMedia variant="icon"><BotIcon /></EmptyMedia><EmptyTitle>No service principals</EmptyTitle><EmptyDescription>Create an automation identity before assigning it an API key.</EmptyDescription></EmptyHeader></Empty>}</Card>
  </PageContainer>
}
