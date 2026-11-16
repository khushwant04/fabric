"use client"

import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { ArrowDownIcon, CheckIcon, ServerIcon, XIcon } from "lucide-react"

import { Panel, PanelAction, PanelContent, PanelHeader, PanelTitle } from "@/components/hexel/panel"
import { Status } from "@/components/hexel/status"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { hostPollDelay, hostStages, hostSummary, isFailureEvent, isFollowing, type Stage } from "@/lib/fabric/host-status"
import type { DeploymentHosts, HostSnapshot } from "@/lib/fabric/types"
import { cn } from "@/lib/utils"
import { formatRelative, shortId } from "@/lib/format"

// Live model-host status for one deployment: a stage timeline per pod, its recent
// Kubernetes events and the tail of the server log, as the stamp's agent last pushed them
// (docs/model-host-logs.md). Polls a console route handler, fast while a host is starting.
export function DeploymentHosts({ deploymentId, initial, initialError }: {
  deploymentId: string
  initial: DeploymentHosts | null
  initialError?: string | null
}) {
  const [data, setData] = useState<DeploymentHosts | null>(initial)
  const [error, setError] = useState<string | null>(initialError ?? null)
  const hosts = data?.hosts ?? null
  const delay = hostPollDelay(hosts)

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    async function poll() {
      if (document.visibilityState === "visible") {
        try {
          const response = await fetch(`/api/deployments/${deploymentId}/hosts`, { cache: "no-store", signal: controller.signal })
          const body = await response.json().catch(() => null)
          if (stopped) return
          if (response.ok) { setData(body as DeploymentHosts); setError(null) }
          else setError((body as { error?: { message?: string } } | null)?.error?.message ?? "Host status could not be read.")
        } catch {
          if (stopped) return
          setError("Host status could not be read. Check your connection.")
        }
      }
      // Rescheduled by the effect when the delay changes; this keeps polling otherwise.
      if (!stopped) timer = setTimeout(poll, delay)
    }
    timer = setTimeout(poll, delay)
    const visible = () => { if (document.visibilityState === "visible") { if (timer) clearTimeout(timer); void poll() } }
    document.addEventListener("visibilitychange", visible)
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer); document.removeEventListener("visibilitychange", visible) }
  }, [deploymentId, delay])

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" aria-live="polite">
      <span>{error ? <span className="text-destructive">{error} Showing the last report.</span> : data?.reported_at ? <>Last host report {formatRelative(data.reported_at)} · checking every {Math.round(delay / 1000)}s</> : "Waiting for the stamp to report its model hosts."}</span>
    </div>
    {hosts && hosts.length ? hosts.map((host) => <HostPanel key={`${host.stamp_id}/${host.pod_name}`} host={host} />) : <Alert><ServerIcon /><AlertTitle>No hosts reported</AlertTitle><AlertDescription>Model-host status appears here once a stamp running this deployment reports its pods. Stamps report hosts when the operator manages them and <code className="font-mono text-xs">agent.reportHosts</code> is on.</AlertDescription></Alert>}
  </div>
}

function HostPanel({ host }: { host: HostSnapshot }) {
  const summary = hostSummary(host)
  const stages = hostStages(host)
  return <Panel>
    <PanelHeader className="flex-wrap">
      <PanelTitle className="min-w-0">
        <Status tone={summary.tone} pulse={summary.tone === "info"}>{summary.label}</Status>
        <span className="truncate font-mono text-xs font-normal text-muted-foreground" title={host.pod_name}>{host.pod_name}</span>
      </PanelTitle>
      <PanelAction className="text-xs text-muted-foreground">
        {host.restart_count ? <span className={cn(host.restart_count >= 3 && "text-destructive")}>{host.restart_count} restart{host.restart_count === 1 ? "" : "s"}</span> : null}
        {host.stale ? <Status tone="warning" className="text-xs">No report {formatRelative(host.reported_at)}</Status> : <span>Reported {formatRelative(host.reported_at)}</span>}
        <span className="font-mono" title={host.stamp_id}>stamp {shortId(host.stamp_id)}</span>
      </PanelAction>
    </PanelHeader>
    <PanelContent>
      <div className="space-y-2 border-b px-4 py-3">
        <StageTimeline stages={stages} />
        {summary.detail ? <p className={cn("text-xs", summary.tone === "critical" ? "text-destructive" : "text-muted-foreground")}>{summary.detail}</p> : null}
      </div>
      <div className="grid lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <EventList host={host} />
        <LogPane host={host} />
      </div>
    </PanelContent>
  </Panel>
}

function StageTimeline({ stages }: { stages: Stage[] }) {
  return <ol className="flex flex-wrap items-center gap-x-1 gap-y-2 text-xs" aria-label="Host start-up stages">
    {stages.map((stage, index) => <li key={stage.key} className="flex items-center gap-1" aria-current={stage.state === "current" ? "step" : undefined}>
      {index ? <span aria-hidden="true" className={cn("mx-1 h-px w-4", stage.state === "pending" ? "bg-border" : "bg-foreground/40")} /> : null}
      <span className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5",
        stage.state === "done" && "border-border text-muted-foreground",
        stage.state === "current" && "border-foreground font-medium text-foreground",
        stage.state === "failed" && "border-destructive font-medium text-destructive",
        stage.state === "pending" && "border-dashed border-border text-muted-foreground/60",
      )}>
        {stage.state === "done" ? <CheckIcon className="size-3" aria-hidden="true" /> : stage.state === "failed" ? <XIcon className="size-3" aria-hidden="true" /> : stage.state === "current" ? <span aria-hidden="true" className="size-1.5 rounded-full bg-info motion-safe:animate-pulse" /> : null}
        {stage.label}
        <span className="sr-only"> ({stage.state})</span>
      </span>
    </li>)}
  </ol>
}

function EventList({ host }: { host: HostSnapshot }) {
  const events = [...host.events].reverse()
  return <div className="min-w-0 border-b lg:border-r lg:border-b-0">
    <div className="px-4 pt-3 pb-1 text-xs text-muted-foreground">Events</div>
    {events.length ? <ul className="max-h-80 overflow-auto px-4 pb-3 text-xs">
      {events.map((event, index) => <li key={`${event.at}-${event.reason}-${index}`} className="border-b border-border/60 py-1.5 last:border-b-0">
        <div className="flex items-baseline justify-between gap-2">
          <span className={cn("font-medium", isFailureEvent(event) && "text-destructive")}>{event.reason}{event.count > 1 ? <span className="font-normal text-muted-foreground"> ×{event.count}</span> : null}</span>
          <time className="shrink-0 tabular-nums text-muted-foreground" dateTime={event.at}>{formatRelative(event.at)}</time>
        </div>
        <p className={cn("mt-0.5 break-words", isFailureEvent(event) ? "text-destructive/90" : "text-muted-foreground")}>{event.message}</p>
      </li>)}
    </ul> : <p className="px-4 pb-3 text-xs text-muted-foreground">No recent events. Kubernetes keeps events for about an hour.</p>}
  </div>
}

function LogPane({ host }: { host: HostSnapshot }) {
  const pane = useRef<HTMLPreElement>(null)
  const [following, setFollowing] = useState(true)
  const followingRef = useRef(true)
  const lineCount = host.log_tail ? host.log_tail.split("\n").length : 0

  // New lines keep the view pinned to the bottom unless the reader scrolled up.
  useLayoutEffect(() => {
    const element = pane.current
    if (element && followingRef.current) element.scrollTop = element.scrollHeight
  }, [host.log_tail])

  function onScroll() {
    const element = pane.current
    if (!element) return
    const next = isFollowing(element.scrollTop, element.clientHeight, element.scrollHeight)
    followingRef.current = next
    setFollowing(next)
  }

  function jump() {
    const element = pane.current
    if (!element) return
    element.scrollTop = element.scrollHeight
    followingRef.current = true
    setFollowing(true)
  }

  return <div className="relative min-w-0">
    <div className="flex items-center justify-between gap-2 px-4 pt-3 pb-1 text-xs text-muted-foreground">
      <span>Log tail{lineCount ? ` · last ${lineCount} line${lineCount === 1 ? "" : "s"}` : ""}{host.log_truncated ? " · trimmed" : ""}{host.log_previous ? <span className="text-destructive"> · previous instance (before restart)</span> : null}</span>
      {!following ? <Button type="button" variant="outline" size="xs" onClick={jump}><ArrowDownIcon />Jump to latest</Button> : null}
    </div>
    <div className="px-4 pb-4">
      {host.log_tail ? <pre ref={pane} onScroll={onScroll} tabIndex={0} aria-label={`Log of ${host.pod_name}`} className="h-80 overflow-auto rounded-xl border bg-muted/40 p-3 font-mono text-[11px] leading-5 whitespace-pre-wrap break-all">{host.log_tail}</pre> : <div className="grid h-24 place-items-center rounded-xl border border-dashed text-xs text-muted-foreground">{host.log_error ? `No log: ${host.log_error}.` : "No log output yet."}</div>}
    </div>
  </div>
}
