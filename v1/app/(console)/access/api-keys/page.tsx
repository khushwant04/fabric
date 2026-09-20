import { KeyRoundIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { CreateApiKeyDialog } from "@/components/console/resource-dialogs"
import { StatusBadge } from "@/components/console/status-badge"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listApiKeys, listServicePrincipals } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate } from "@/lib/format"

export default async function ApiKeysPage() {
  const [context, keys, principals] = await Promise.all([getConsoleContext(), listApiKeys(), listServicePrincipals()])
  const canWrite = hasScope(context, "api-keys:write") && !context.demo
  return <PageContainer><PageHeader title="API keys" description="Issue narrowly scoped credentials for automation and development workflows." actions={<CreateApiKeyDialog principals={principals} disabled={!canWrite} />} />
    <Card><CardHeader><CardTitle>Account credentials</CardTitle><CardDescription>Secrets are returned only at creation. Revoke keys that are no longer needed.</CardDescription></CardHeader>{keys.length ? <CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Key</TableHead><TableHead>Owner type</TableHead><TableHead>Scopes</TableHead><TableHead>Status</TableHead><TableHead>Expires</TableHead><TableHead className="pr-6 text-right">Created</TableHead></TableRow></TableHeader><TableBody>{keys.map((key) => <TableRow key={key.id}><TableCell className="pl-6 font-medium">{key.name}<div className="font-mono text-[11px] font-normal text-muted-foreground">{key.key_prefix}••••</div></TableCell><TableCell className="capitalize">{key.principal_type.replaceAll("_", " ")}</TableCell><TableCell><div className="flex max-w-sm flex-wrap gap-1">{key.scopes.slice(0, 3).map((scope) => <Badge variant="secondary" key={scope} className="font-mono text-[10px]">{scope}</Badge>)}{key.scopes.length > 3 ? <Badge variant="outline">+{key.scopes.length - 3}</Badge> : null}</div></TableCell><TableCell><StatusBadge status={key.revoked_at ? "revoked" : "active"} /></TableCell><TableCell className="text-muted-foreground">{key.expires_at ? formatDate(key.expires_at) : "Never"}</TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatDate(key.created_at)}</TableCell></TableRow>)}</TableBody></Table></CardContent> : <Empty className="min-h-72 border-0"><EmptyHeader><EmptyMedia variant="icon"><KeyRoundIcon /></EmptyMedia><EmptyTitle>No API keys</EmptyTitle><EmptyDescription>Create a scoped credential for an operator or service principal.</EmptyDescription></EmptyHeader></Empty>}</Card>
  </PageContainer>
}
