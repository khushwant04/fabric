import Link from "next/link"
import { ActivityIcon, ArrowRightIcon, BoxesIcon, CpuIcon, FlaskConicalIcon, GaugeIcon, KeyRoundIcon, PlusIcon, TriangleAlertIcon } from "lucide-react"

import { LiveRefresh } from "@/components/console/live-refresh"
import { StatusBadge } from "@/components/console/status-badge"
import { PhaseChart } from "@/components/dashboard/phase-chart"
import { ActionCard, SectionCard, StatCard } from "@/components/dashboard/surfaces"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { getAccountUsage, getDeploymentStatuses, listDeployments, listPlacements, listStamps } from "@/lib/fabric/data"
import { deploymentState, gpuCapacity, stampState } from "@/lib/fabric/resource-state"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatNumber, formatRelative } from "@/lib/format"

function timestamp(value: string | null) {
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isNaN(parsed) ? 0 : parsed
}

export default async function DashboardPage() {
  const context = await getConsoleContext()
  const canReadDeployments = hasScope(context, "deployments:read")
  const canReadStamps = hasScope(context, "stamps:read")
  const [deploymentResult, stampResult, usageResult] = await Promise.allSettled([
    canReadDeployments ? listDeployments() : Promise.resolve(null),
    canReadStamps ? listStamps() : Promise.resolve(null),
    canReadDeployments ? getAccountUsage() : Promise.resolve(null),
  ])
  const deploymentData = deploymentResult.status === "fulfilled" ? deploymentResult.value : null
  const stampData = stampResult.status === "fulfilled" ? stampResult.value : null
  const usage = usageResult.status === "fulfilled" ? usageResult.value : null
  const deployments = deploymentData ?? []
  const stamps = stampData ?? []
  const observed = []
  for (let index = 0; index < deployments.length; index += 4) {
    observed.push(...await Promise.all(deployments.slice(index, index + 4).map(async (deployment) => {
      try {
        const [statuses, placements] = await Promise.all([getDeploymentStatuses(deployment.id), listPlacements(deployment.id)])
        return { deployment, state: deploymentState(deployment, statuses, placements, stampData ?? undefined), failed: false }
      } catch {
        return { deployment, state: deploymentState(deployment), failed: true }
      }
    })))
  }
  const error = [deploymentResult, stampResult, usageResult].some((result) => result.status === "rejected") || observed.some((item) => item.failed)
    ? "Some account data could not be checked." : null
  const checkedAt = new Date().toISOString()
  const ready = observed.filter((item) => item.state.status === "ready").length
  const attention = observed.filter((item) => ["degraded", "failed", "error", "unverified", "waiting"].includes(item.state.status)).length
  const phaseData = Object.entries(observed.reduce<Record<string, number>>((result, item) => {
    result[item.state.status] = (result[item.state.status] ?? 0) + 1
    return result
  }, {})).map(([phase, count]) => ({ phase, deployments: count }))
  const stampRows = stamps.map((stamp) => ({ stamp, state: stampState(stamp) }))
  const activeStamps = stampRows.filter((item) => item.state.status === "active")
  const capacityKnown = stampData !== null && activeStamps.length > 0 && activeStamps.every(({ stamp }) => gpuCapacity(stamp.capabilities.allocatable_gpus) !== null && gpuCapacity(stamp.capabilities.requested_gpus) !== null)
  const totalGpu = activeStamps.reduce((sum, { stamp }) => sum + (gpuCapacity(stamp.capabilities.allocatable_gpus) ?? 0), 0)
  const usedGpu = activeStamps.reduce((sum, { stamp }) => sum + (gpuCapacity(stamp.capabilities.requested_gpus) ?? 0), 0)
  const recent = [...observed].sort((left, right) => timestamp(right.deployment.updated_at) - timestamp(left.deployment.updated_at))
  const canCreate = hasScope(context, "deployments:write")

  return <div className="mx-auto w-full max-w-[1480px] space-y-7 px-5 py-7 sm:px-7 lg:px-9 lg:py-9">
    <div className="flex flex-wrap items-start justify-between gap-5">
      <div><p className="mb-2 text-[11px] font-medium uppercase tracking-[.12em] text-muted-foreground">Your inference workspace</p><h1 className="text-[28px] font-semibold leading-9 tracking-[-.035em]">Overview</h1><p className="mt-2 text-[13px] leading-6 text-muted-foreground">Deployment health, GPU capacity, and inference usage in one place.</p></div>
      <div className="flex flex-wrap items-center gap-2 pt-1"><Button nativeButton={false} variant="outline" className="h-9 rounded-full px-4 text-xs" render={<Link href="/playground" />}><FlaskConicalIcon />Open playground</Button>{canCreate ? <Button nativeButton={false} className="h-9 rounded-full px-4 text-xs" render={<Link href="/deployments/new" />}><PlusIcon />Create deployment</Button> : null}</div>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-3 border-y py-3"><div className="flex items-center gap-2 text-xs text-muted-foreground"><span className={`size-1.5 rounded-full ${attention ? "bg-amber-500" : ready ? "bg-emerald-500" : "bg-muted-foreground"}`} />{deploymentData === null ? "Deployment data unavailable" : `${ready} of ${deployments.length} deployments observed ready`}{attention ? ` · ${attention} need review` : ""}</div><LiveRefresh checkedAt={checkedAt} error={error} /></div>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <StatCard label="Deployments" value={deploymentData === null ? "Unavailable" : deployments.length} detail={deploymentData === null ? "Deployment data could not be checked" : `${ready} observed ready`} icon={<BoxesIcon />} />
      <StatCard label="Active stamps" value={stampData === null ? "Unavailable" : activeStamps.length} detail={stampData === null ? "Stamp data could not be checked" : `${stamps.length} registered in this account`} icon={<CpuIcon />} />
      <StatCard label="Inference requests" value={usage ? formatNumber(usage.events) : "Unavailable"} detail={usage ? usage.last_occurred_at ? `Last usage ${formatRelative(usage.last_occurred_at)}` : "No usage reported yet" : "Usage data could not be checked"} icon={<ActivityIcon />} />
      <StatCard label="Tokens processed" value={usage ? formatNumber(usage.input_tokens + usage.output_tokens) : "Unavailable"} detail={usage ? `${formatNumber(usage.input_tokens)} input · ${formatNumber(usage.output_tokens)} output` : "Usage data could not be checked"} icon={<GaugeIcon />} />
    </div>
    {attention ? <Link href="/deployments" className="flex flex-wrap items-center gap-3 rounded-xl border border-amber-500/20 bg-amber-500/5 px-4 py-3 text-[13px]"><TriangleAlertIcon className="size-4 text-amber-600" /><span>{attention} deployment{attention === 1 ? " needs" : "s need"} a closer look. Review replica readiness and placement observations.</span><ArrowRightIcon className="ml-auto size-4 text-muted-foreground" /></Link> : null}
    <div className="grid gap-4 xl:grid-cols-[1.25fr_1fr]">
      <SectionCard title="Deployment health" description="Observed serving state across your deployments." action={<Link href="/deployments" className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">View deployments<ArrowRightIcon className="size-3.5" /></Link>}>
        {phaseData.length ? <PhaseChart data={phaseData} /> : <div className="flex h-56 items-center justify-center text-sm text-muted-foreground">{deploymentData === null ? "Deployment health is unavailable." : "Create a deployment to see its serving state."}</div>}
      </SectionCard>
      <SectionCard title="GPU capacity" description="Capacity reported by active account-owned stamps." action={<Link href="/stamps" className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">View stamps<ArrowRightIcon className="size-3.5" /></Link>}>
        {capacityKnown ? <div className="mb-5"><div className="mb-3 flex items-end justify-between"><span className="text-[28px] font-semibold tracking-tight tabular-nums">{usedGpu}<span className="ml-1 text-sm font-normal text-muted-foreground">/ {totalGpu} GPUs requested</span></span><span className="text-xs text-muted-foreground">{totalGpu ? Math.round(usedGpu / totalGpu * 100) : 0}%</span></div><Progress value={totalGpu ? Math.min(100, usedGpu / totalGpu * 100) : 0} className="h-1.5" /></div> : <p className="mb-5 rounded-xl bg-muted/40 p-4 text-xs leading-5 text-muted-foreground">Complete GPU capacity has not been reported by every active stamp.</p>}
        {stampRows.length ? <div className="divide-y">{stampRows.slice(0, 3).map(({ stamp, state }) => <div className="flex items-center justify-between gap-3 py-3" key={stamp.id}><div className="flex min-w-0 items-center gap-3"><span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted/60"><CpuIcon className="size-4 text-muted-foreground" /></span><div className="min-w-0"><p className="truncate text-[13px] font-medium">{stamp.name}</p><p className="mt-0.5 truncate text-xs text-muted-foreground">{stamp.region || "Region unavailable"} · Heartbeat {formatRelative(stamp.last_heartbeat_at)}</p></div></div><StatusBadge status={state.status} /></div>)}</div> : <p className="py-8 text-center text-sm text-muted-foreground">{stampData === null ? "Stamp data is unavailable." : "No account-owned stamps are registered."}</p>}
      </SectionCard>
    </div>
    <SectionCard title="Recent deployments" description="Latest changes and their observed serving status." action={<Link href="/deployments" className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">View all<ArrowRightIcon className="size-3.5" /></Link>} className="[&>div:last-child]:px-0 [&>div:last-child]:pt-4">
      {recent.length ? <Table><TableHeader><TableRow className="bg-muted/25"><TableHead className="pl-5 text-xs sm:pl-6">Deployment</TableHead><TableHead className="text-xs">Status</TableHead><TableHead className="hidden text-xs md:table-cell">Capacity</TableHead><TableHead className="pr-5 text-right text-xs sm:pr-6">Updated</TableHead></TableRow></TableHeader><TableBody>{recent.slice(0, 5).map(({ deployment, state }) => <TableRow key={deployment.id}><TableCell className="py-4 pl-5 sm:pl-6"><Link href={`/deployments/${deployment.id}`} className="text-[13px] font-medium hover:underline">{deployment.name}</Link><p className="mt-1 text-xs text-muted-foreground">{deployment.model_alias}</p></TableCell><TableCell><StatusBadge status={state.status} /><p className="mt-1 max-w-64 text-[11px] leading-4 text-muted-foreground">{state.detail}</p></TableCell><TableCell className="hidden text-xs text-muted-foreground md:table-cell">{deployment.desired_spec.replicas} replica{deployment.desired_spec.replicas === 1 ? "" : "s"} · {deployment.desired_spec.resources.gpu_class.toUpperCase()}</TableCell><TableCell className="pr-5 text-right text-xs text-muted-foreground sm:pr-6">{formatRelative(deployment.updated_at)}</TableCell></TableRow>)}</TableBody></Table> : <div className="py-12 text-center"><BoxesIcon className="mx-auto mb-3 size-6 text-muted-foreground" /><p className="text-sm font-medium">{deploymentData === null ? "Deployment data is unavailable" : "Your first deployment starts here"}</p><p className="mt-1 text-xs text-muted-foreground">{deploymentData === null ? "Refresh to check your account deployments again." : "Choose a model and let Fabric find compatible capacity."}</p></div>}
    </SectionCard>
    <section className="space-y-3"><h2 className="text-[15px] font-medium tracking-tight">Explore your workspace</h2><div className="grid gap-3 md:grid-cols-3"><ActionCard title="Inference playground" description="Compare two model responses, streaming speed, and routing decisions." href="/playground" icon={<FlaskConicalIcon />} /><ActionCard title="API credentials" description="Create scoped keys for applications and service principals." href="/access/api-keys" icon={<KeyRoundIcon />} /><ActionCard title="Usage and activity" description="Review collector-reported request and token totals." href="/usage" icon={<ActivityIcon />} /></div></section>
    <p className="text-[11px] leading-5 text-muted-foreground">Operational usage is collector-reported. GPU capacity covers account-owned stamps and excludes shared managed capacity.</p>
  </div>
}
