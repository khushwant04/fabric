import Link from "next/link"
import { notFound } from "next/navigation"
import { ArrowLeftIcon, TriangleAlertIcon } from "lucide-react"

import { CredentialRotationButton } from "@/components/console/credential-rotation"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StampActivity } from "@/components/console/stamp-activity"
import { StampMetricsView } from "@/components/console/serving-metrics/stamp-metrics"
import { RemoveStampButton } from "@/components/console/stamp-remove"
import { Section, SectionBody } from "@/components/console/section"
import { StatusBadge } from "@/components/console/status-badge"
import { TruncatedText } from "@/components/console/truncated-text"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { describeAccelerator, deviceNoun, isNeuronEntry, isTpuEntry, stampAcceleratorKind } from "@/lib/fabric/accelerators"
import { getStampEvents, listDeployments, listStamps } from "@/lib/fabric/data"
import { publicGatewayBase } from "@/lib/fabric/inference-endpoints"
import { gpuCapacity, stampState } from "@/lib/fabric/resource-state"
import { isRemoved, isStampId } from "@/lib/fabric/stamp-activity"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate, formatRelative, orchestratorLabel } from "@/lib/format"

export default async function StampDetailPage({ params }: PageProps<"/stamps/[stampId]">) {
  const { stampId } = await params
  if (!isStampId(stampId)) notFound()
  const context = await getConsoleContext()
  const [stampResult, eventResult, deploymentResult] = await Promise.allSettled([listStamps(), getStampEvents(stampId), hasScope(context, "deployments:read") ? listDeployments() : Promise.resolve([])])
  const stamp = stampResult.status === "fulfilled" ? stampResult.value.find((item) => item.id === stampId) : undefined
  if (stampResult.status === "fulfilled" && !stamp) notFound()
  const events = eventResult.status === "fulfilled" ? eventResult.value : null
  const canWrite = hasScope(context, "stamps:write")
  const name = stamp?.name ?? "Inference stamp"
  const gone = stamp ? isRemoved(stamp) : Boolean(events?.revoked_at)
  const state = stamp ? (gone ? { status: "removed", detail: `Removed ${formatRelative(stamp.revoked_at)}; its credentials are revoked and its agent no longer syncs.` } : stampState(stamp)) : null
  const gpus = Array.isArray(stamp?.capabilities.gpus) ? stamp.capabilities.gpus as Array<Record<string, unknown>> : []
  const acceleratorKind = stampAcceleratorKind(stamp?.capabilities)
  const endpoint = publicGatewayBase(stamp?.capabilities.inference_url)
  // Names for the GPUs the metrics attribute to a deployment (docs/serving-metrics.md).
  const deploymentNames = Object.fromEntries((deploymentResult.status === "fulfilled" ? deploymentResult.value : []).map((deployment) => [deployment.id, deployment.name]))

  return <PageContainer>
    <PageHeader title={name} description="Registration, hardware, the live activity log and serving metrics reported by this stamp's agent." actions={<><Button variant="outline" render={<Link href="/stamps" />}><ArrowLeftIcon /> All stamps</Button>{stamp && !gone ? <RemoveStampButton stampId={stamp.id} name={stamp.name} disabled={!canWrite} size="default" variant="destructive" label="Remove stamp" /> : null}</>}>
      {state ? <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-border bg-muted/40 px-4 py-3"><StatusBadge status={state.status} /><span className="min-w-0 text-sm text-muted-foreground">{state.detail}</span></div> : null}
    </PageHeader>
    {stampResult.status === "rejected" ? <Alert variant="destructive"><TriangleAlertIcon /><AlertTitle>Stamp details are unavailable</AlertTitle><AlertDescription>Refresh to try again. The activity log below still updates.</AlertDescription></Alert> : null}
    {state?.status === "stale" && stamp ? <Alert><TriangleAlertIcon /><AlertTitle>No heartbeat for over 5 minutes</AlertTitle><AlertDescription><p>The machine may be off or offline, or its agent stopped. Deployments placed here are not served until it reports again. If you no longer use this machine, remove it.</p>{canWrite ? <div className="mt-2"><RemoveStampButton stampId={stamp.id} name={stamp.name} size="xs" /></div> : null}</AlertDescription></Alert> : null}
    <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <Section title="Activity" description="Enrollment, hardware checks, credential rotations, applied generations, image pulls and errors.">
        <SectionBody><StampActivity stampId={stampId} initial={events} initialError={eventResult.status === "rejected" ? "Activity could not be read." : null} /></SectionBody>
      </Section>
      {stamp ? <Section title="Stamp" description="As last reported by its agent.">
        <dl className="flex flex-col border-t border-border text-sm">
          <StackField label={deviceNoun(acceleratorKind)}>{gpus.length ? <ul className="flex flex-col gap-1">{gpus.map((gpu, index) => isTpuEntry(gpu) ? <li key={index}>{describeAccelerator(gpu)}{typeof gpu.accelerator_type === "string" ? ` · ${gpu.accelerator_type}` : ""}{typeof gpu.memory_bytes === "number" && gpu.memory_bytes > 0 ? <span className="text-muted-foreground"> · {Math.round(gpu.memory_bytes / 1024 ** 3)} GiB HBM per chip</span> : null}</li> : isNeuronEntry(gpu) ? <li key={index}>{describeAccelerator(gpu)}{typeof gpu.accelerator_type === "string" ? ` · ${gpu.accelerator_type}` : ""}{typeof gpu.memory_bytes === "number" && gpu.memory_bytes > 0 ? <span className="text-muted-foreground"> · {Math.round(gpu.memory_bytes / 1024 ** 3)} GiB HBM per device</span> : null}</li> : <li key={index}><span className="tabular-nums">{gpuCapacity(gpu.count) ?? "?"} ×</span> {typeof gpu.product === "string" ? gpu.product : "Unknown GPU"}{typeof gpu.memory_bytes === "number" && gpu.memory_bytes > 0 ? <span className="text-muted-foreground tabular-nums"> · {Math.round(gpu.memory_bytes / 1024 ** 3)} GiB</span> : null}</li>)}</ul> : <span className="text-muted-foreground">None reported</span>}</StackField>
          <StackField label="GPU capacity"><span className="tabular-nums">{gpuCapacity(stamp.capabilities.requested_gpus) ?? "—"} requested</span><span className="text-muted-foreground tabular-nums"> of {gpuCapacity(stamp.capabilities.allocatable_gpus) ?? "—"} allocatable</span></StackField>
          <StackField label="Orchestrator">{orchestratorLabel(stamp.orchestrator)}<span className="text-muted-foreground"> · {stamp.region || "no region"}</span></StackField>
          <StackField label="Last heartbeat"><time dateTime={stamp.last_heartbeat_at ?? undefined} title={formatDate(stamp.last_heartbeat_at)}>{formatRelative(stamp.last_heartbeat_at)}</time></StackField>
          <StackField label="Enrolled">{formatDate(stamp.created_at)}</StackField>
          <StackField label="Inference gateway">{endpoint ? <TruncatedText text={endpoint} className="font-mono text-[13px]" /> : <span className="text-muted-foreground">Not reported</span>}</StackField>
          <StackField label="Stamp ID"><TruncatedText text={stamp.id} className="font-mono text-[13px]" /></StackField>
          <StackField label="Machine credentials"><p>Version {stamp.credential_version ?? 1}<span className="text-muted-foreground"> · {stamp.credentials_rotated_at ? `rotated ${formatRelative(stamp.credentials_rotated_at)}` : "never rotated"}</span></p>{gone ? null : <div className="mt-2.5"><CredentialRotationButton stampId={stamp.id} pending={Boolean(stamp.credential_rotation_requested_at)} disabled={!canWrite} /></div>}</StackField>
        </dl>
      </Section> : null}
    </div>
    <StampMetricsView stampId={stampId} deploymentNames={deploymentNames} />
  </PageContainer>
}

/** A label above its value, for the narrow stamp card. */
function StackField({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="flex min-w-0 flex-col gap-1 border-b border-border px-6 py-3.5 last:border-b-0">
    <dt className="text-[13px] text-muted-foreground">{label}</dt>
    <dd className="min-w-0">{children}</dd>
  </div>
}
