import { CpuIcon, GaugeIcon, RadioIcon } from "lucide-react"

import { EnrollmentTokenDialog } from "@/components/console/resource-dialogs"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge } from "@/components/console/status-badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listStamps } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatRelative, shortId } from "@/lib/format"

export default async function StampsPage() {
  const [context, stamps] = await Promise.all([getConsoleContext(), listStamps()])
  const active = stamps.filter((item) => item.status === "active" && !item.revoked_at).length
  const allocatable = stamps.reduce((sum, item) => sum + Number(item.capabilities.allocatable_gpus ?? 0), 0)
  const requested = stamps.reduce((sum, item) => sum + Number(item.capabilities.requested_gpus ?? 0), 0)
  const canWrite = hasScope(context, "stamps:write") && !context.demo

  return <PageContainer><PageHeader title="Inference stamps" description="Connect and monitor the infrastructure that reconciles Fabric deployments." actions={<EnrollmentTokenDialog disabled={!canWrite} />} />
    <div className="grid gap-5 sm:grid-cols-3">{[{ label: "Registered stamps", value: stamps.length, icon: CpuIcon }, { label: "Active", value: active, icon: RadioIcon }, { label: "Allocatable GPUs", value: allocatable, icon: GaugeIcon }].map((item) => <Card key={item.label}><CardHeader><CardDescription className="flex items-center justify-between">{item.label}<item.icon className="size-4" /></CardDescription><CardTitle className="text-2xl">{item.value}</CardTitle></CardHeader></Card>)}</div>
    <Card><CardHeader><CardTitle>Infrastructure</CardTitle><CardDescription>Heartbeat and capabilities reported by account-owned agents.</CardDescription></CardHeader>{stamps.length ? <CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Stamp</TableHead><TableHead>Status</TableHead><TableHead>Region</TableHead><TableHead>Orchestrator</TableHead><TableHead>GPU capacity</TableHead><TableHead className="pr-6 text-right">Last heartbeat</TableHead></TableRow></TableHeader><TableBody>{stamps.map((stamp) => { const total = Number(stamp.capabilities.allocatable_gpus ?? 0); const used = Number(stamp.capabilities.requested_gpus ?? 0); return <TableRow key={stamp.id}><TableCell className="pl-6 font-medium">{stamp.name}<div className="font-mono text-[11px] font-normal text-muted-foreground">{shortId(stamp.id)}</div></TableCell><TableCell><StatusBadge status={stamp.revoked_at ? "revoked" : stamp.status} /></TableCell><TableCell>{stamp.region || "—"}</TableCell><TableCell className="capitalize">{stamp.orchestrator || "Unknown"}</TableCell><TableCell><div className="w-36"><div className="mb-1 flex justify-between text-xs"><span>{used} requested</span><span className="text-muted-foreground">{total}</span></div><Progress value={total ? used / total * 100 : 0} className="h-1.5" /></div></TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatRelative(stamp.last_heartbeat_at)}</TableCell></TableRow>})}</TableBody></Table></CardContent> : <Empty className="min-h-80 border-0"><EmptyHeader><EmptyMedia variant="icon"><CpuIcon /></EmptyMedia><EmptyTitle>No stamps connected</EmptyTitle><EmptyDescription>Create an enrollment token, then configure the Fabric agent in your Kubernetes cluster.</EmptyDescription></EmptyHeader></Empty>}</Card>
    {allocatable > 0 ? <Card><CardHeader><CardTitle>Fleet utilization</CardTitle><CardDescription>{requested} of {allocatable} allocatable GPUs are currently requested.</CardDescription></CardHeader><CardContent><Progress value={requested / allocatable * 100} /></CardContent></Card> : null}
  </PageContainer>
}
