import Link from "next/link"
import { ActivityIcon, ArrowDownToLineIcon, ArrowUpFromLineIcon, InfoIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Metric, MetricGrid, Section, SectionEmpty } from "@/components/console/section"
import { TruncatedText } from "@/components/console/truncated-text"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { getDeploymentUsage, listDeployments } from "@/lib/fabric/data"
import { formatDate, formatNumber } from "@/lib/format"

export default async function UsagePage() {
  const deployments = await listDeployments()
  const rows = await Promise.all(deployments.map(async (deployment) => ({ deployment, usage: await getDeploymentUsage(deployment.id) })))
  const totals = rows.reduce((sum, row) => ({ events: sum.events + row.usage.events, input: sum.input + row.usage.input_tokens, output: sum.output + row.usage.output_tokens }), { events: 0, input: 0, output: 0 })
  return <PageContainer><PageHeader title="Usage" description="Operational inference totals reported by collectors for each deployment." />
    <Alert><InfoIcon /><AlertTitle>Operational data</AlertTitle><AlertDescription>These totals are deduplicated per stamp but are not a billing ledger. Date-range analytics require a historical telemetry API.</AlertDescription></Alert>
    <MetricGrid className="md:grid-cols-3">{[{ label: "Inference events", value: totals.events, icon: ActivityIcon }, { label: "Input tokens", value: totals.input, icon: ArrowDownToLineIcon }, { label: "Output tokens", value: totals.output, icon: ArrowUpFromLineIcon }].map((item) => <Metric key={item.label} label={item.label} value={formatNumber(item.value)} detail={`Across ${rows.length} deployment${rows.length === 1 ? "" : "s"}`} icon={<item.icon />} />)}</MetricGrid>
    <Section title="Usage by deployment" description="Lifetime totals currently retained by the control plane.">{rows.length ? <Table className="min-w-[720px]"><TableHeader><TableRow><TableHead>Deployment</TableHead><TableHead className="text-right">Events</TableHead><TableHead className="text-right">Input tokens</TableHead><TableHead className="text-right">Output tokens</TableHead><TableHead className="text-right">Last reported</TableHead></TableRow></TableHeader><TableBody>{rows.map(({ deployment, usage }) => <TableRow key={deployment.id}><TableCell className="h-14 font-medium"><Link href={`/deployments/${deployment.id}`} className="block max-w-64 hover:underline"><TruncatedText text={deployment.name} /></Link><TruncatedText text={deployment.model_alias} className="max-w-64 font-mono text-xs font-normal text-muted-foreground" /></TableCell>{[usage.events, usage.input_tokens, usage.output_tokens].map((value, index) => <TableCell key={index} className={value ? "text-right tabular-nums" : "text-right text-muted-foreground tabular-nums"}>{formatNumber(value)}</TableCell>)}<TableCell className="text-right text-muted-foreground tabular-nums">{formatDate(usage.last_occurred_at)}</TableCell></TableRow>)}</TableBody></Table> : <SectionEmpty>No deployments have reported usage yet.</SectionEmpty>}</Section>
  </PageContainer>
}
