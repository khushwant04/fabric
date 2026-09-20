import Link from "next/link"
import { ActivityIcon, ArrowRightIcon, BoxesIcon, CpuIcon, GaugeIcon, PlusIcon, TriangleAlertIcon } from "lucide-react"

import { PhaseChart } from "@/components/dashboard/phase-chart"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { getDeploymentUsage, listDeployments, listStamps } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatNumber, formatRelative } from "@/lib/format"

export default async function DashboardPage() {
  const context = await getConsoleContext()
  const [deployments, stamps] = await Promise.all([listDeployments(), listStamps()])
  const usage = await Promise.all(deployments.slice(0, 8).map((item) => getDeploymentUsage(item.id)))
  const totals = usage.reduce((result, item) => ({ events: result.events + item.events, tokens: result.tokens + item.input_tokens + item.output_tokens }), { events: 0, tokens: 0 })
  const ready = deployments.filter((item) => ["ready", "active"].includes(item.status.toLowerCase())).length
  const attention = deployments.filter((item) => ["degraded", "failed", "error"].includes(item.status.toLowerCase())).length
  const liveStamps = stamps.filter((item) => item.status === "active" && !item.revoked_at).length
  const phaseData = Object.entries(deployments.reduce<Record<string, number>>((result, item) => { result[item.status] = (result[item.status] ?? 0) + 1; return result }, {})).map(([phase, count]) => ({ phase, deployments: count }))
  const totalGpu = stamps.reduce((sum, stamp) => sum + Number(stamp.capabilities.allocatable_gpus ?? 0), 0)
  const usedGpu = stamps.reduce((sum, stamp) => sum + Number(stamp.capabilities.requested_gpus ?? 0), 0)

  return (
    <PageContainer>
      <PageHeader title="Overview" description="Monitor deployment health, connected infrastructure, and operational usage across this account." actions={hasScope(context, "deployments:write") ? <Button render={<Link href="/deployments/new" />}><PlusIcon /> Create deployment</Button> : undefined} />
      {attention > 0 ? <Alert><TriangleAlertIcon /><AlertTitle>{attention} deployment{attention === 1 ? "" : "s"} need attention</AlertTitle><AlertDescription>Open deployment status to compare desired and observed generations.</AlertDescription></Alert> : null}

      <Card className="gap-0 py-0">
        <div className="grid divide-y sm:grid-cols-2 sm:divide-x sm:divide-y-0 xl:grid-cols-4">
          {[{ label: "Deployments", value: deployments.length, meta: `${ready} ready`, icon: BoxesIcon }, { label: "Active stamps", value: liveStamps, meta: `${stamps.length} registered`, icon: CpuIcon }, { label: "Inference events", value: formatNumber(totals.events), meta: "Operational total", icon: ActivityIcon }, { label: "Tokens processed", value: formatNumber(totals.tokens), meta: "Reported by collectors", icon: GaugeIcon }].map((metric) => <div className="p-5" key={metric.label}><div className="flex items-center justify-between text-xs text-muted-foreground"><span>{metric.label}</span><metric.icon className="size-4" /></div><div className="mt-3 text-2xl font-semibold tabular-nums">{metric.value}</div><div className="mt-1 text-xs text-muted-foreground">{metric.meta}</div></div>)}
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[1.55fr_1fr]">
        <Card>
          <CardHeader><CardTitle>Deployment health</CardTitle><CardDescription>Current control-plane phase by deployment.</CardDescription><CardAction><Button variant="ghost" size="sm" render={<Link href="/deployments" />}>View all <ArrowRightIcon /></Button></CardAction></CardHeader>
          <CardContent>{phaseData.length ? <PhaseChart data={phaseData} /> : <div className="flex h-56 items-center justify-center text-sm text-muted-foreground">No deployment data yet.</div>}</CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>GPU capacity</CardTitle><CardDescription>Reported capacity across account-owned stamps.</CardDescription></CardHeader>
          <CardContent className="gap-5"><div><div className="mb-2 flex justify-between text-sm"><span>{usedGpu} GPU requested</span><span className="text-muted-foreground">{totalGpu} allocatable</span></div><Progress value={totalGpu ? (usedGpu / totalGpu) * 100 : 0} /></div><div className="divide-y rounded-lg border">{stamps.slice(0, 4).map((stamp) => <div key={stamp.id} className="flex items-center justify-between gap-3 px-3 py-2.5"><div className="min-w-0"><div className="truncate text-sm font-medium">{stamp.name}</div><div className="text-xs text-muted-foreground">{stamp.region || "Region unavailable"}</div></div><StatusBadge status={stamp.status} /></div>)}</div></CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Recent deployments</CardTitle><CardDescription>Latest desired-state changes for this account.</CardDescription></CardHeader>
        <CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Name</TableHead><TableHead>Model</TableHead><TableHead>Status</TableHead><TableHead>Resources</TableHead><TableHead className="pr-6 text-right">Updated</TableHead></TableRow></TableHeader><TableBody>{deployments.slice(0, 6).map((deployment) => <TableRow key={deployment.id}><TableCell className="pl-6 font-medium"><Link href={`/deployments/${deployment.id}`} className="hover:underline">{deployment.name}</Link></TableCell><TableCell className="text-muted-foreground">{deployment.model_alias}</TableCell><TableCell><StatusBadge status={deployment.status} /></TableCell><TableCell>{deployment.desired_spec.replicas} × {deployment.desired_spec.resources.gpu_class.toUpperCase()}</TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatRelative(deployment.updated_at)}</TableCell></TableRow>)}</TableBody></Table></CardContent>
      </Card>
    </PageContainer>
  )
}
