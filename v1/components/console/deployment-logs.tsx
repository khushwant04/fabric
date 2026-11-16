"use client"

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { ArrowDownIcon, CheckIcon, DownloadIcon, InfoIcon, PauseIcon, PlayIcon, SearchIcon, ServerIcon, TriangleAlertIcon, XIcon } from "lucide-react"

import { Panel, PanelAction, PanelContent, PanelHeader, PanelTitle } from "@/components/hexel/panel"
import { Status } from "@/components/hexel/status"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import {
  diagnoseHost, elapsedSeconds, formatBytes, formatDuration, hostPollDelay, hostStages, hostSummary,
  isFailureEvent, isFollowing, logFileName, logLines, pullInfo, queuedStages, stageStartedAt, weightProgress,
  type LogLevel, type Stage, isStartupWait } from "@/lib/fabric/host-status"
import type { DeploymentHosts, HostSnapshot } from "@/lib/fabric/types"
import { formatRelative, shortId } from "@/lib/format"
import { cn } from "@/lib/utils"

/** Where the deployment is before any model host exists. */
export type QueueState = { state: "waiting" | "placed" | "removed"; stampName?: string | null; action?: React.ReactNode }

// The deployment's Logs tab: a start-up stage timeline with real progress for every model-host
// pod (image pull with size and elapsed time, weight download in bytes, loading, warm-up),
// failures in red with a hint, and a full-height raw log with follow, pause, search, filter
// and download. Data is what the stamp's agent last pushed (docs/model-host-logs.md), polled
// through a console route handler, fast while a host is starting.
export function DeploymentLogs({ deploymentId, initial, initialError, queue }: {
  deploymentId: string
  initial: DeploymentHosts | null
  initialError?: string | null
  queue: QueueState
}) {
  const [data, setData] = useState<DeploymentHosts | null>(initial)
  const [error, setError] = useState<string | null>(initialError ?? null)
  const [selected, setSelected] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const hosts = useMemo(() => data?.hosts ?? [], [data])
  const delay = hostPollDelay(data?.hosts)
  const starting = hosts.some((host) => !host.ready)

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
      if (!stopped) timer = setTimeout(poll, delay)
    }
    timer = setTimeout(poll, delay)
    const visible = () => { if (document.visibilityState === "visible") { if (timer) clearTimeout(timer); void poll() } }
    document.addEventListener("visibilitychange", visible)
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer); document.removeEventListener("visibilitychange", visible) }
  }, [deploymentId, delay])

  // A one-second clock for elapsed times, only while something is starting.
  useEffect(() => {
    if (!starting) return
    const interval = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(interval)
  }, [starting])

  const key = (host: HostSnapshot) => `${host.stamp_id}/${host.pod_name}`
  const current = hosts.find((host) => key(host) === selected) ?? hosts[0] ?? null

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" aria-live="polite">
      <span>{error ? <span className="text-destructive">{error} Showing the last report.</span> : data?.reported_at ? <>Last report from the stamp {formatRelative(data.reported_at)} · checking every {Math.round(delay / 1000)}s</> : "Waiting for the stamp to report its model host."}</span>
      {hosts.length > 1 ? <div className="flex flex-wrap gap-1" role="tablist" aria-label="Model hosts">{hosts.map((host) => <Button key={key(host)} type="button" size="xs" role="tab" aria-selected={current === host} variant={current === host ? "default" : "outline"} onClick={() => setSelected(key(host))}><span className="font-mono">{host.pod_name.slice(-10)}</span></Button>)}</div> : null}
    </div>
    {current ? <HostLogs key={key(current)} host={current} now={now} /> : <QueuedPanel queue={queue} />}
  </div>
}

function QueuedPanel({ queue }: { queue: QueueState }) {
  const stages = queuedStages(queue.state)
  return <Panel>
    <PanelHeader><PanelTitle><Status tone={queue.state === "removed" ? "critical" : "info"} pulse={queue.state !== "removed"}>{queue.state === "removed" ? "Stamp removed" : stages[0].label}</Status></PanelTitle></PanelHeader>
    <PanelContent>
      <div className="space-y-4 p-4">
        <StageTimeline stages={stages} details={{ queued: queue.state === "waiting" ? "Waiting for a placement on compatible capacity" : queue.state === "placed" ? `Placed on ${queue.stampName ?? "a stamp"}; its agent creates the model host on its next poll (every 15 s)` : "The stamp this deployment was placed on was removed" }} />
        {queue.state === "removed" ? <Alert variant="destructive"><TriangleAlertIcon /><AlertTitle>Nothing is serving this deployment</AlertTitle><AlertDescription><p>Its stamp was removed and its credentials revoked. Redeploy it onto another stamp, or delete the deployment.</p>{queue.action ? <div className="mt-2">{queue.action}</div> : null}</AlertDescription></Alert> : <Alert><ServerIcon /><AlertTitle>No model host yet</AlertTitle><AlertDescription>Progress appears here as soon as the stamp starts the model host: scheduling, the model runtime image pull, the weight download, loading and warm-up, with the server&apos;s log below.</AlertDescription></Alert>}
      </div>
    </PanelContent>
  </Panel>
}

function HostLogs({ host, now }: { host: HostSnapshot; now: number }) {
  const summary = hostSummary(host)
  const stages = hostStages(host)
  const diagnosis = diagnoseHost(host)
  const pull = pullInfo(host)
  const weights = weightProgress(host)
  const currentKey = stages.find((stage) => stage.state === "current" || stage.state === "failed")?.key

  const pullDetail = pull.state === "pulled"
    ? [formatBytes(pull.sizeBytes), pull.seconds !== null ? `took ${formatDuration(pull.seconds)}` : ""].filter(Boolean).join(" · ")
    : pull.state === "present" ? "Already on this machine"
      : pull.state === "pulling" ? `${formatDuration(elapsedSeconds(pull.startedAt, now))} elapsed · size shown when it finishes`
        : pull.state === "failed" ? "Pull failed; retrying" : ""
  const downloadStarted = stageStartedAt(host, "downloading")
  const downloadDetail = weights
    ? weights.total ? `${formatBytes(weights.downloaded)} of ${formatBytes(weights.total)} (${Math.floor((weights.fraction ?? 0) * 100)}%)` : `${formatBytes(weights.downloaded)} so far · total size unknown`
    : currentKey === "downloading" ? `${formatDuration(elapsedSeconds(downloadStarted, now))} elapsed · progress not measurable on this stamp` : ""

  return <Panel>
    <PanelHeader className="flex-wrap">
      <PanelTitle className="min-w-0">
        <Status tone={summary.tone} pulse={summary.tone === "info"}>{summary.label}</Status>
        <span className="truncate font-mono text-xs font-normal text-muted-foreground" title={host.pod_name}>{host.pod_name}</span>
      </PanelTitle>
      <PanelAction className="text-xs text-muted-foreground">
        {host.started_at && !host.ready ? <span className="tabular-nums">Started {formatDuration(elapsedSeconds(host.started_at, now))} ago</span> : null}
        {host.restart_count ? <span className={cn(host.restart_count >= 3 && "text-destructive")}>{host.restart_count} restart{host.restart_count === 1 ? "" : "s"}</span> : null}
        {host.stale ? <Status tone="warning" className="text-xs">No report {formatRelative(host.reported_at)}</Status> : <span>Reported {formatRelative(host.reported_at)}</span>}
        <span className="font-mono" title={host.stamp_id}>stamp {shortId(host.stamp_id)}</span>
      </PanelAction>
    </PanelHeader>
    <PanelContent>
      <div className="space-y-4 border-b p-4">
        <StageTimeline stages={stages} details={{ pulling: pullDetail, downloading: downloadDetail }} progress={weights?.fraction ?? null} />
        {diagnosis ? <Alert variant="destructive"><TriangleAlertIcon /><AlertTitle>{diagnosis.title}</AlertTitle><AlertDescription>{diagnosis.hint}</AlertDescription></Alert> : null}
        {!diagnosis && pull.state === "pulling" ? <Alert><InfoIcon /><AlertTitle>First deployment on this machine</AlertTitle><AlertDescription>The first deployment on a machine downloads the model runtime image once{pull.sizeBytes ? ` (${formatBytes(pull.sizeBytes)})` : " (several GB)"}. Later deployments on this machine start from the local copy and skip this step.</AlertDescription></Alert> : null}
        {!diagnosis && summary.detail && summary.tone !== "critical" ? <p className="text-xs text-muted-foreground">{summary.detail}</p> : null}
      </div>
      <LogViewer host={host} />
      <EventList host={host} />
    </PanelContent>
  </Panel>
}

function StageTimeline({ stages, details, progress = null }: { stages: Stage[]; details: Partial<Record<Stage["key"], string>>; progress?: number | null }) {
  return <ol className="grid gap-x-2 gap-y-3 sm:grid-cols-2 xl:grid-cols-7" aria-label="Start-up stages">
    {stages.map((stage) => <li key={stage.key} aria-current={stage.state === "current" ? "step" : undefined} className={cn(
      "min-w-0 border-t-2 pt-2",
      stage.state === "done" && "border-foreground/40",
      stage.state === "current" && "border-info",
      stage.state === "failed" && "border-destructive",
      stage.state === "pending" && "border-border",
    )}>
      <div className={cn("flex items-center gap-1.5 text-xs",
        stage.state === "done" && "text-muted-foreground",
        stage.state === "current" && "font-medium text-foreground",
        stage.state === "failed" && "font-medium text-destructive",
        stage.state === "pending" && "text-muted-foreground/60",
      )}>
        {stage.state === "done" ? <CheckIcon className="size-3 shrink-0" aria-hidden="true" /> : stage.state === "failed" ? <XIcon className="size-3 shrink-0" aria-hidden="true" /> : stage.state === "current" ? <span aria-hidden="true" className="size-1.5 shrink-0 bg-info motion-safe:animate-pulse" /> : null}
        <span>{stage.label}</span>
        <span className="sr-only"> ({stage.state})</span>
      </div>
      {details[stage.key] && stage.state !== "pending" ? <p className="mt-1 text-[11px] leading-4 text-muted-foreground tabular-nums">{details[stage.key]}</p> : null}
      {stage.key === "downloading" && progress !== null && stage.state !== "pending" ? <Progress value={Math.round(progress * 100)} aria-label="Weight download progress" className="mt-1.5" /> : null}
    </li>)}
  </ol>
}

function highlight(text: string, query: string) {
  const needle = query.trim()
  if (!needle) return text
  const parts: React.ReactNode[] = []
  const lower = text.toLowerCase()
  const target = needle.toLowerCase()
  let index = 0
  while (index < text.length) {
    const found = lower.indexOf(target, index)
    if (found === -1) { parts.push(text.slice(index)); break }
    if (found > index) parts.push(text.slice(index, found))
    parts.push(<mark key={found} className="bg-info/25 text-foreground">{text.slice(found, found + needle.length)}</mark>)
    index = found + needle.length
  }
  return parts
}

function LogViewer({ host }: { host: HostSnapshot }) {
  const pane = useRef<HTMLDivElement>(null)
  const followingRef = useRef(true)
  const [following, setFollowing] = useState(true)
  const [paused, setPaused] = useState<string | null>(null)
  const [query, setQuery] = useState("")
  const [level, setLevel] = useState<LogLevel>("all")
  const text = paused ?? host.log_tail ?? ""
  const lines = useMemo(() => logLines(text, query, level), [text, query, level])
  const total = text ? text.split("\n").length : 0
  const newSincePause = paused !== null && host.log_tail ? Math.max(0, host.log_tail.split("\n").length - total) : 0

  // New lines keep the view pinned to the bottom unless the reader scrolled up or paused.
  useLayoutEffect(() => {
    const element = pane.current
    if (element && followingRef.current) element.scrollTop = element.scrollHeight
  }, [lines])

  function onScroll() {
    const element = pane.current
    if (!element) return
    const next = isFollowing(element.scrollTop, element.clientHeight, element.scrollHeight)
    followingRef.current = next
    setFollowing(next)
  }

  function jump() {
    const element = pane.current
    if (element) element.scrollTop = element.scrollHeight
    followingRef.current = true
    setFollowing(true)
    setPaused(null)
  }

  function download() {
    const blob = new Blob([host.log_tail ?? ""], { type: "text/plain;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = logFileName(host.pod_name)
    link.click()
    URL.revokeObjectURL(url)
  }

  return <div className="border-b">
    <div className="flex flex-wrap items-center gap-2 px-4 pt-3 pb-2">
      <div className="relative min-w-48 flex-1">
        <SearchIcon aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search the log" aria-label="Search the log" className="h-7 pl-7 text-xs" />
      </div>
      <div className="flex" role="group" aria-label="Filter lines">
        <Button type="button" size="sm" variant={level === "all" ? "default" : "outline"} aria-pressed={level === "all"} onClick={() => setLevel("all")}>All lines</Button>
        <Button type="button" size="sm" variant={level === "problems" ? "default" : "outline"} aria-pressed={level === "problems"} onClick={() => setLevel("problems")}>Warnings &amp; errors</Button>
      </div>
      <Button type="button" size="sm" variant="outline" onClick={() => setPaused(paused === null ? host.log_tail ?? "" : null)} aria-pressed={paused !== null}>{paused === null ? <><PauseIcon />Pause</> : <><PlayIcon />Resume{newSincePause ? ` (${newSincePause} new)` : ""}</>}</Button>
      <Button type="button" size="sm" variant="outline" onClick={download} disabled={!host.log_tail}><DownloadIcon />Download</Button>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-1 text-xs text-muted-foreground">
      <span>{total ? `${query || level !== "all" ? `${lines.length} of ` : ""}${total} line${total === 1 ? "" : "s"} · the last 200 the stamp sent` : "Server log"}{host.log_truncated ? " · trimmed" : ""}{host.log_previous ? <span className="text-destructive"> · previous instance (before restart)</span> : null}{paused !== null ? " · paused" : following ? " · following" : ""}</span>
      {!following || paused !== null ? <Button type="button" variant="outline" size="xs" onClick={jump}><ArrowDownIcon />Jump to latest</Button> : null}
    </div>
    <div className="px-4 pb-4">
      {text ? <div ref={pane} onScroll={onScroll} tabIndex={0} role="log" aria-label={`Log of ${host.pod_name}`} className="h-[65vh] min-h-80 overflow-auto border bg-muted/40 py-2 font-mono text-[11px] leading-5">
        {lines.length ? lines.map((line) => <div key={line.number} className={cn("grid grid-cols-[3.5rem_minmax(0,1fr)] px-2 hover:bg-muted", line.level === "error" && "text-destructive", line.level === "warning" && "text-foreground")}>
          <span aria-hidden="true" className="pr-3 text-right text-muted-foreground/60 select-none tabular-nums">{line.number}</span>
          <span className="break-all whitespace-pre-wrap">{line.level === "warning" ? <span aria-hidden="true" className="mr-1 inline-block size-1.5 bg-status-warning align-middle" /> : null}{highlight(line.text, query)}</span>
        </div>) : <p className="px-3 text-muted-foreground">No lines match.</p>}
      </div> : <div className="grid h-40 place-items-center border border-dashed px-4 text-center text-xs text-muted-foreground">{host.log_error ? `No log yet: ${host.log_error}. The server's output appears here once its container starts.` : "No log output yet."}</div>}
    </div>
  </div>
}

function EventList({ host }: { host: HostSnapshot }) {
  const events = [...host.events].reverse()
  return <details className="group px-4 py-3 text-xs" open={events.some(isFailureEvent)}>
    <summary className="cursor-pointer text-muted-foreground select-none">Kubernetes events ({events.length})</summary>
    {events.length ? <ul className="mt-2 max-h-72 overflow-auto">
      {events.map((event, index) => <li key={`${event.at}-${event.reason}-${index}`} className="border-b border-border/60 py-1.5 last:border-b-0">
        <div className="flex items-baseline justify-between gap-2">
          <span className={cn("font-medium", isFailureEvent(event) && "text-destructive")}>{event.reason}{event.count > 1 ? <span className="font-normal text-muted-foreground"> ×{event.count}</span> : null}</span>
          <time className="shrink-0 tabular-nums text-muted-foreground" dateTime={event.at}>{formatRelative(event.at)}</time>
        </div>
        <p className={cn("mt-0.5 break-words", isFailureEvent(event) ? "text-destructive/90" : "text-muted-foreground")}>{event.message}</p>
        {isStartupWait(event) ? <p className="mt-0.5 text-muted-foreground">Expected while the model starts: the server opens its port once the weights are loaded.</p> : null}
      </li>)}
    </ul> : <p className="mt-2 text-muted-foreground">No recent events. Kubernetes keeps events for about an hour.</p>}
  </details>
}
