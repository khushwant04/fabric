import { CpuIcon, GaugeIcon, RadioIcon, ServerIcon } from "lucide-react"

import { CredentialRotationButton } from "@/components/console/credential-rotation"
import { EdgeGatewayCard } from "@/components/console/edge-gateway"
import { EnrollmentTokenDrawer } from "@/components/console/stamp-enrollment"
import { LiveRefresh } from "@/components/console/live-refresh"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Field, FieldList, IconTile, Metric, MetricGrid, Section, SectionBody, SectionEmpty } from "@/components/console/section"
import { TruncatedText } from "@/components/console/truncated-text"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listStamps } from "@/lib/fabric/data"
import { gpuCapacity, stampState } from "@/lib/fabric/resource-state"
import { publicGatewayBase } from "@/lib/fabric/inference-endpoints"
import { consoleUsesEdge, edgeGatewayUrl } from "@/lib/fabric/playground-data"
import type { Stamp } from "@/lib/fabric/types"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { getStampEnrollmentConfig } from "@/lib/fabric/stamp-enrollment-config"
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
  const edgeUrl = edgeGatewayUrl()
  const routable = activeStamps.filter((item) => publicGatewayBase(item.capabilities.inference_url) !== null).length

  return <PageContainer><PageHeader title="Inference stamps" description="Connect and monitor the infrastructure that reconciles Fabric deployments." actions={<EnrollmentTokenDrawer disabled={!canWrite} config={getStampEnrollmentConfig()} />} />
    <LiveRefresh checkedAt={checkedAt} error={error} />
    {error ? <Alert variant="destructive"><CpuIcon /><AlertTitle>Unable to load stamps</AlertTitle><AlertDescription>Refresh to try again. Fleet totals are unavailable.</AlertDescription></Alert> : null}
    <MetricGrid className="sm:grid-cols-3">{[{ label: "Registered stamps", value: error ? "—" : stamps.length, detail: error ? "Unavailable" : `${stamps.length - active} without a recent heartbeat`, icon: CpuIcon }, { label: "Active", value: error ? "—" : active, detail: error ? "Unavailable" : "Recent heartbeat", icon: RadioIcon }, { label: "Allocatable GPUs", value: error || incompleteCapacity ? "—" : allocatable, detail: error || incompleteCapacity ? "Unavailable · not every active stamp reported" : "Across active stamps", icon: GaugeIcon }].map((item) => <Metric key={item.label} label={item.label} value={item.value} detail={item.detail} icon={<item.icon />} />)}</MetricGrid>
    {edgeUrl ? <EdgeGatewayCard url={edgeUrl} usedByConsole={consoleUsesEdge()} stamps={error ? null : routable} /> : null}
    <Section title="Infrastructure" description="Heartbeat and capabilities reported by account-owned agents.">{stamps.length ? <Table className="min-w-[900px]"><TableHeader><TableRow><TableHead>Stamp</TableHead><TableHead>Status</TableHead><TableHead>Region</TableHead><TableHead>Orchestrator</TableHead><TableHead>GPU capacity</TableHead><TableHead className="text-right">Last heartbeat</TableHead></TableRow></TableHeader><TableBody>{stamps.map((stamp) => { const total = gpuCapacity(stamp.capabilities.allocatable_gpus); const used = gpuCapacity(stamp.capabilities.requested_gpus); const state = states.get(stamp.id)!; return <TableRow key={stamp.id}><TableCell className="h-14 font-medium"><TruncatedText text={stamp.name} className="max-w-52" /><div className="font-mono text-[11px] font-normal text-muted-foreground">{shortId(stamp.id)}</div></TableCell><TableCell className="min-w-56 whitespace-normal"><StatusBadge status={state.status} /><p className="mt-0.5 max-w-xs text-xs text-muted-foreground">{state.detail}</p></TableCell><TableCell>{stamp.region || "—"}</TableCell><TableCell className="capitalize">{stamp.orchestrator || "Unknown"}</TableCell><TableCell><div className="w-40"><div className="mb-1.5 flex justify-between text-xs tabular-nums"><span>{used ?? "—"} requested</span><span className="text-muted-foreground">of {total ?? "—"}</span></div><Progress value={total && used !== null ? Math.min(100, used / total * 100) : 0} className="h-1.5" /></div>{state.status !== "active" ? <p className="mt-1 text-[11px] text-muted-foreground">Last reported · excluded from active totals</p> : null}</TableCell><TableCell className="text-right text-muted-foreground tabular-nums">{formatRelative(stamp.last_heartbeat_at)}</TableCell></TableRow>})}</TableBody></Table> : !error ? <Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><CpuIcon /></EmptyMedia><EmptyTitle>No stamps connected</EmptyTitle><EmptyDescription>Create an enrollment token, then configure the Fabric agent in your Kubernetes cluster.</EmptyDescription></EmptyHeader></Empty> : <SectionEmpty>Stamp data is unavailable.</SectionEmpty>}</Section>
    {stamps.length ? <div className="grid items-start gap-4 lg:grid-cols-2">{stamps.map((stamp) => {
      const reported = Array.isArray(stamp.capabilities.gpus) ? stamp.capabilities.gpus as Array<Record<string, unknown>> : []
      const endpoint = publicGatewayBase(stamp.capabilities.inference_url)
      return <Section key={stamp.id} title={<><IconTile tone="neutral" className="size-8 rounded-lg [&_svg]:size-4"><ServerIcon /></IconTile><span className="truncate">{stamp.name}</span></>} action={<StatusBadge status={states.get(stamp.id)!.status} />}>
        <FieldList>
          <Field label="GPU inventory">{reported.length ? <ul className="flex flex-col gap-2">{reported.map((gpu, index) => <li key={index}><div>{typeof gpu.product === "string" ? gpu.product : "Unknown GPU"} <span className="text-muted-foreground tabular-nums">× {gpuCapacity(gpu.count) ?? "Unknown"}</span></div><p className="mt-0.5 text-xs font-normal text-muted-foreground">{typeof gpu.memory_bytes === "number" && gpu.memory_bytes > 0 ? `${(gpu.memory_bytes / 1024 ** 3).toFixed(1)} GiB per GPU` : "Memory unreported"} · compute {typeof gpu.compute_capability === "string" ? gpu.compute_capability : "unreported"} · source {typeof gpu.source === "string" && gpu.source ? gpu.source : "agent inventory; measurement source unreported"}</p></li>)}</ul> : <span className="font-normal text-muted-foreground">No GPU inventory reported. Check the node driver, NVIDIA device plugin, and GPU feature discovery.</span>}</Field>
          <Field label="Inference gateway" mono>{endpoint ?? <span className="font-sans text-muted-foreground">Not reported; configure stamp gateway exposure in Helm.</span>}</Field>
          <Field label="Machine credentials"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-normal"><span className="tabular-nums">Version {stamp.credential_version ?? 1}</span> <span className="text-muted-foreground">· {stamp.credentials_rotated_at ? `rotated ${formatRelative(stamp.credentials_rotated_at)}` : "never rotated"}</span></span>{stamp.revoked_at ? null : <CredentialRotationButton stampId={stamp.id} pending={Boolean(stamp.credential_rotation_requested_at)} disabled={!canWrite} />}</div></Field>
        </FieldList>
      </Section>
    })}</div> : null}
    {!incompleteCapacity && allocatable > 0 ? <Section title="GPU reservations" action={<span className="text-xs text-muted-foreground tabular-nums">{requested} of {allocatable} requested</span>}><SectionBody><Progress value={Math.min(100, requested / allocatable * 100)} className="h-2" /><p className="text-xs text-muted-foreground">{requested} of {allocatable} allocatable GPUs are requested on stamps with a recent heartbeat. Reservations are separate from GPU compute activity.</p></SectionBody></Section> : null}
  </PageContainer>
}
