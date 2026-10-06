import { CpuIcon, GaugeIcon, RadioIcon } from "lucide-react"

import { EnrollmentTokenDialog } from "@/components/console/resource-dialogs"
import { LiveRefresh } from "@/components/console/live-refresh"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listStamps } from "@/lib/fabric/data"
import { gpuCapacity, stampState } from "@/lib/fabric/resource-state"
import type { Stamp } from "@/lib/fabric/types"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatRelative, shortId } from "@/lib/format"

export default async function StampsPage() {
  const context = await getConsoleContext()
  let stamps: Stamp[] = []
  let error: string | null = null
  try { stamps = await listStamps() } catch { error = "Stamp data is unavailable." }
  const checkedAt = new Date().toISOString()
  const states = new Map(stamps.map((stamp) => [stamp.id, stampState(stamp)]))
  const activeStamps = stamps.filter((item) => states.get(item.id)!.status === "active")
  const active = activeStamps.length
  const allocatable = activeStamps.reduce((sum, item) => sum + (gpuCapacity(item.capabilities.allocatable_gpus) ?? 0), 0)
  const requested = activeStamps.reduce((sum, item) => sum + (gpuCapacity(item.capabilities.requested_gpus) ?? 0), 0)
  const incompleteCapacity = activeStamps.some((item) => gpuCapacity(item.capabilities.allocatable_gpus) === null || gpuCapacity(item.capabilities.requested_gpus) === null)
  const canWrite = hasScope(context, "stamps:write")

  return <PageContainer><PageHeader title="Inference stamps" description="Connect and monitor the infrastructure that reconciles Fabric deployments." actions={<EnrollmentTokenDialog disabled={!canWrite} />} />
    <LiveRefresh checkedAt={checkedAt} error={error} />
    {error ? <Alert variant="destructive"><CpuIcon /><AlertTitle>Unable to load stamps</AlertTitle><AlertDescription>Refresh to try again. Fleet totals are unavailable.</AlertDescription></Alert> : null}
    <div className="grid gap-5 sm:grid-cols-3">{[{ label: "Registered stamps", value: error ? "Unavailable" : stamps.length, icon: CpuIcon }, { label: "Active · recent heartbeat", value: error ? "Unavailable" : active, icon: RadioIcon }, { label: "Allocatable GPUs · active stamps", value: error || incompleteCapacity ? "Unavailable" : allocatable, icon: GaugeIcon }].map((item) => <Card key={item.label}><CardHeader><CardDescription className="flex items-center justify-between">{item.label}<item.icon className="size-4" /></CardDescription><CardTitle className="text-2xl">{item.value}</CardTitle></CardHeader></Card>)}</div>
    <Card><CardHeader><CardTitle>Infrastructure</CardTitle><CardDescription>Heartbeat and capabilities reported by account-owned agents.</CardDescription></CardHeader>{stamps.length ? <CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Stamp</TableHead><TableHead>Status</TableHead><TableHead>Region</TableHead><TableHead>Orchestrator</TableHead><TableHead>GPU capacity</TableHead><TableHead className="pr-6 text-right">Last heartbeat</TableHead></TableRow></TableHeader><TableBody>{stamps.map((stamp) => { const total = gpuCapacity(stamp.capabilities.allocatable_gpus); const used = gpuCapacity(stamp.capabilities.requested_gpus); const state = states.get(stamp.id)!; return <TableRow key={stamp.id}><TableCell className="pl-6 font-medium">{stamp.name}<div className="font-mono text-[11px] font-normal text-muted-foreground">{shortId(stamp.id)}</div></TableCell><TableCell><StatusBadge status={state.status} /><p className="mt-1 max-w-xs text-xs text-muted-foreground">{state.detail}</p></TableCell><TableCell>{stamp.region || "—"}</TableCell><TableCell className="capitalize">{stamp.orchestrator || "Unknown"}</TableCell><TableCell><div className="w-36"><div className="mb-1 flex justify-between text-xs"><span>{used ?? "—"} requested</span><span className="text-muted-foreground">{total ?? "—"}</span></div><Progress value={total && used !== null ? Math.min(100, used / total * 100) : 0} className="h-1.5" /></div>{state.status !== "active" ? <p className="mt-1 text-[11px] text-muted-foreground">Last reported · excluded from active totals</p> : null}</TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatRelative(stamp.last_heartbeat_at)}</TableCell></TableRow>})}</TableBody></Table></CardContent> : !error ? <Empty className="min-h-80 border-0"><EmptyHeader><EmptyMedia variant="icon"><CpuIcon /></EmptyMedia><EmptyTitle>No stamps connected</EmptyTitle><EmptyDescription>Create an enrollment token, then configure the Fabric agent in your Kubernetes cluster.</EmptyDescription></EmptyHeader></Empty> : null}</Card>
    {!incompleteCapacity && allocatable > 0 ? <Card><CardHeader><CardTitle>Fleet utilization</CardTitle><CardDescription>{requested} of {allocatable} allocatable GPUs are requested on stamps with a recent heartbeat.</CardDescription></CardHeader><CardContent><Progress value={Math.min(100, requested / allocatable * 100)} /></CardContent></Card> : null}
  </PageContainer>
}
