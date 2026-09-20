import { ActivityIcon, InfoIcon, TextCursorInputIcon, TextCursorIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { getDeploymentUsage, listDeployments } from "@/lib/fabric/data"
import { formatDate, formatNumber } from "@/lib/format"

export default async function UsagePage() {
  const deployments = await listDeployments()
  const rows = await Promise.all(deployments.map(async (deployment) => ({ deployment, usage: await getDeploymentUsage(deployment.id) })))
  const totals = rows.reduce((sum, row) => ({ events: sum.events + row.usage.events, input: sum.input + row.usage.input_tokens, output: sum.output + row.usage.output_tokens }), { events: 0, input: 0, output: 0 })
  return <PageContainer><PageHeader title="Usage" description="Operational inference totals reported by collectors for each deployment." />
    <Alert><InfoIcon /><AlertTitle>Operational data</AlertTitle><AlertDescription>These totals are deduplicated per stamp but are not a billing ledger. Date-range analytics require a historical telemetry API.</AlertDescription></Alert>
    <div className="grid gap-5 md:grid-cols-3">{[{ label: "Inference events", value: totals.events, icon: ActivityIcon }, { label: "Input tokens", value: totals.input, icon: TextCursorInputIcon }, { label: "Output tokens", value: totals.output, icon: TextCursorIcon }].map((item) => <Card key={item.label}><CardHeader><CardDescription className="flex items-center justify-between">{item.label}<item.icon className="size-4" /></CardDescription><CardTitle className="text-2xl">{formatNumber(item.value)}</CardTitle></CardHeader></Card>)}</div>
    <Card><CardHeader><CardTitle>Usage by deployment</CardTitle><CardDescription>Lifetime totals currently retained by the control plane.</CardDescription></CardHeader><CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Deployment</TableHead><TableHead>Events</TableHead><TableHead>Input tokens</TableHead><TableHead>Output tokens</TableHead><TableHead className="pr-6 text-right">Last reported</TableHead></TableRow></TableHeader><TableBody>{rows.map(({ deployment, usage }) => <TableRow key={deployment.id}><TableCell className="pl-6 font-medium">{deployment.name}<div className="text-xs font-normal text-muted-foreground">{deployment.model_alias}</div></TableCell><TableCell>{formatNumber(usage.events)}</TableCell><TableCell>{formatNumber(usage.input_tokens)}</TableCell><TableCell>{formatNumber(usage.output_tokens)}</TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatDate(usage.last_occurred_at)}</TableCell></TableRow>)}</TableBody></Table></CardContent></Card>
  </PageContainer>
}
