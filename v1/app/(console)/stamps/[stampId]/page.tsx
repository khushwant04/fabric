import Link from "next/link"
import { notFound } from "next/navigation"
import { ArrowLeftIcon, TriangleAlertIcon } from "lucide-react"

import { CredentialRotationButton } from "@/components/console/credential-rotation"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StampActivity } from "@/components/console/stamp-activity"
import { RemoveStampButton } from "@/components/console/stamp-remove"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { getStampEvents, listStamps } from "@/lib/fabric/data"
import { publicGatewayBase } from "@/lib/fabric/inference-endpoints"
import { gpuCapacity, stampState } from "@/lib/fabric/resource-state"
import { isRemoved, isStampId } from "@/lib/fabric/stamp-activity"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate, formatRelative } from "@/lib/format"

export default async function StampDetailPage({ params }: PageProps<"/stamps/[stampId]">) {
  const { stampId } = await params
  if (!isStampId(stampId)) notFound()
  const context = await getConsoleContext()
  const [stampResult, eventResult] = await Promise.allSettled([listStamps(), getStampEvents(stampId)])
  const stamp = stampResult.status === "fulfilled" ? stampResult.value.find((item) => item.id === stampId) : undefined
  if (stampResult.status === "fulfilled" && !stamp) notFound()
  const events = eventResult.status === "fulfilled" ? eventResult.value : null
  const canWrite = hasScope(context, "stamps:write")
  const name = stamp?.name ?? "Inference stamp"
  const gone = stamp ? isRemoved(stamp) : Boolean(events?.revoked_at)
  const state = stamp ? (gone ? { status: "removed", detail: `Removed ${formatRelative(stamp.revoked_at)}; its credentials are revoked and its agent no longer syncs.` } : stampState(stamp)) : null
  const gpus = Array.isArray(stamp?.capabilities.gpus) ? stamp.capabilities.gpus as Array<Record<string, unknown>> : []
  const endpoint = publicGatewayBase(stamp?.capabilities.inference_url)

  return <PageContainer>
    <PageHeader title={name} description="Registration, hardware and the live activity log reported by this stamp's agent." actions={<><Button variant="outline" render={<Link href="/stamps" />}><ArrowLeftIcon /> All stamps</Button>{stamp && !gone ? <RemoveStampButton stampId={stamp.id} name={stamp.name} disabled={!canWrite} size="default" variant="destructive" label="Remove stamp" /> : null}</>}>
      {state ? <div className="flex flex-wrap items-center gap-2 pt-1"><StatusBadge status={state.status} /><span className="text-xs text-muted-foreground">{state.detail}</span></div> : null}
    </PageHeader>
    {stampResult.status === "rejected" ? <Alert variant="destructive"><TriangleAlertIcon /><AlertTitle>Stamp details are unavailable</AlertTitle><AlertDescription>Refresh to try again. The activity log below still updates.</AlertDescription></Alert> : null}
    {state?.status === "stale" && stamp ? <Alert><TriangleAlertIcon /><AlertTitle>No heartbeat for over 5 minutes</AlertTitle><AlertDescription><p>The machine may be off or offline, or its agent stopped. Deployments placed here are not served until it reports again. If you no longer use this machine, remove it.</p>{canWrite ? <div className="mt-2"><RemoveStampButton stampId={stamp.id} name={stamp.name} size="xs" /></div> : null}</AlertDescription></Alert> : null}
    <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <Card>
        <CardHeader><CardTitle>Activity</CardTitle><CardDescription>Enrollment, hardware checks, credential rotations, applied generations, image pulls and errors. Newest first.</CardDescription></CardHeader>
        <CardContent><StampActivity stampId={stampId} initial={events} initialError={eventResult.status === "rejected" ? "Activity could not be read." : null} /></CardContent>
      </Card>
      {stamp ? <Card>
        <CardHeader><CardTitle>Stamp</CardTitle><CardDescription>As last reported by its agent.</CardDescription></CardHeader>
        <CardContent>
          <dl className="grid gap-4 text-sm">
            <div><dt className="text-xs text-muted-foreground">GPUs</dt><dd className="mt-1 space-y-1">{gpus.length ? gpus.map((gpu, index) => <div key={index}>{gpuCapacity(gpu.count) ?? "?"} × {typeof gpu.product === "string" ? gpu.product : "Unknown GPU"}{typeof gpu.memory_bytes === "number" && gpu.memory_bytes > 0 ? ` · ${Math.round(gpu.memory_bytes / 1024 ** 3)} GiB` : ""}</div>) : <span className="text-muted-foreground">None reported</span>}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Allocatable · requested</dt><dd className="mt-1 tabular-nums">{gpuCapacity(stamp.capabilities.allocatable_gpus) ?? "—"} · {gpuCapacity(stamp.capabilities.requested_gpus) ?? "—"}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Orchestrator · region</dt><dd className="mt-1"><span className="capitalize">{stamp.orchestrator || "Unknown"}</span> · {stamp.region || "no region"}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Last heartbeat</dt><dd className="mt-1">{formatRelative(stamp.last_heartbeat_at)}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Enrolled</dt><dd className="mt-1">{formatDate(stamp.created_at)}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Inference gateway</dt><dd className="mt-1 break-all font-mono text-xs">{endpoint ?? "Not reported"}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Stamp ID</dt><dd className="mt-1 break-all font-mono text-xs">{stamp.id}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Machine credentials</dt><dd className="mt-1 space-y-2"><p>Version {stamp.credential_version ?? 1} · {stamp.credentials_rotated_at ? `rotated ${formatRelative(stamp.credentials_rotated_at)}` : "never rotated"}</p>{gone ? null : <CredentialRotationButton stampId={stamp.id} pending={Boolean(stamp.credential_rotation_requested_at)} disabled={!canWrite} />}</dd></div>
          </dl>
        </CardContent>
      </Card> : null}
    </div>
  </PageContainer>
}
