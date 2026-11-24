"use client"

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { ArrowDownToLineIcon, CheckIcon, ChevronRightIcon, DownloadIcon, InfoIcon, PauseIcon, PlayIcon, SearchIcon, ServerIcon, TerminalIcon, TriangleAlertIcon, WrapTextIcon, XIcon } from "lucide-react"

import { MetaLine } from "@/components/console/meta-line"
import { Segmented } from "@/components/console/segmented"
import { TruncatedText } from "@/components/console/truncated-text"
import { Panel, PanelContent, PanelHeader } from "@/components/hexel/panel"
import { Status } from "@/components/hexel/status"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import {
  diagnoseHost, elapsedSeconds, formatBytes, formatDuration, hostPollDelay, hostStages, hostSummary,
  isFailureEvent, isFollowing, logFileName, logLines, pullInfo, queuedStages, stageStartedAt, weightProgress,
  type LogLevel, type LogLine, type Stage, isStartupWait } from "@/lib/fabric/host-status"
import type { DeploymentHosts, HostSnapshot } from "@/lib/fabric/types"
import { formatRelative, shortId } from "@/lib/format"
import { cn } from "@/lib/utils"

/** Where the deployment is before any model host exists. */
export type QueueState = { state: "waiting" | "placed" | "removed"; stampName?: string | null; action?: React.ReactNode }

// The deployment's Logs tab: a start-up stage timeline with real progress for every model-host
// pod (image pull with size and elapsed time, weight download in bytes, loading, warm-up),
// failures in red with a hint, and a log console with follow, pause, search, filter, line
// wrapping and download. Data is what the stamp's agent last pushed (docs/model-host-logs.md),
// polled through a console route handler, fast while a host is starting.
export function DeploymentLogs({ deploymentId, initial, initialError, queue, stampNames = {} }: {
  deploymentId: string
  initial: DeploymentHosts | null
  initialError?: string | null
  queue: QueueState
  /** Stamp names by id, to name the machine a host runs on instead of showing its id. */
  stampNames?: Record<string, string>
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

  return <div className="flex flex-col gap-4">
    <div className="flex flex-wrap items-center justify-between gap-3" aria-live="polite">
      {error
        ? <p className="text-xs text-destructive">{error} Showing the last report.</p>
        : data?.reported_at
          ? <MetaLine><span>Last report from the stamp {formatRelative(data.reported_at)}</span><span>Checking every {Math.round(delay / 1000)}s</span></MetaLine>
          : <p className="text-xs text-muted-foreground">Waiting for the stamp to report its model host.</p>}
      {hosts.length > 1 && current ? <Segmented label="Model hosts" value={key(current)} onChange={setSelected}
        options={hosts.map((host, index) => ({
          value: key(host),
          ariaLabel: `Replica ${index + 1}, ${host.pod_name}`,
          label: <><Status tone={hostSummary(host).tone} className="gap-1.5 text-[13px]">Replica {index + 1}</Status><span className="font-mono text-[11px] text-muted-foreground">{host.pod_name.slice(-5)}</span></>,
        }))} /> : null}
    </div>
    {current ? <HostLogs key={key(current)} host={current} now={now} stampName={stampNames[current.stamp_id]} /> : <QueuedPanel queue={queue} />}
  </div>
}

function QueuedPanel({ queue }: { queue: QueueState }) {
  const stages = queuedStages(queue.state)
  const removed = queue.state === "removed"
  return <Panel>
    <PanelHeader><Status tone={removed ? "critical" : "info"} pulse={!removed} className="font-medium">{removed ? "Stamp removed" : stages[0].label}</Status></PanelHeader>
    <PanelContent>
      <div className="flex flex-col gap-5 p-5">
        <StageTimeline stages={stages} details={{ queued: queue.state === "waiting" ? "Waiting for compatible capacity" : queue.state === "placed" ? `Placed on ${queue.stampName ?? "a stamp"}; its agent starts the model host on its next poll (every 15 s)` : "The stamp this deployment was placed on was removed" }} />
        {removed
          ? <Alert variant="destructive"><TriangleAlertIcon /><AlertTitle>Nothing is serving this deployment</AlertTitle><AlertDescription><p>Its stamp was removed and its credentials revoked. Redeploy it onto another stamp, or delete the deployment.</p>{queue.action ? <div className="mt-2">{queue.action}</div> : null}</AlertDescription></Alert>
          : <Alert><ServerIcon /><AlertTitle>No model host yet</AlertTitle><AlertDescription>Progress appears here as soon as the stamp starts the model host: scheduling, the model runtime image pull, the weight download, loading and warm-up, with the server&apos;s log below.</AlertDescription></Alert>}
      </div>
    </PanelContent>
  </Panel>
}

function HostLogs({ host, now, stampName }: { host: HostSnapshot; now: number; stampName?: string }) {
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
  const failed = summary.tone === "critical"

  return <Panel>
    <PanelHeader className="flex-wrap gap-x-4 gap-y-1.5 py-3">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Status tone={summary.tone} pulse={summary.tone === "info"} className="font-medium">{summary.label}</Status>
          {failed && host.container_reason ? <span className="rounded-md border border-status-critical/25 bg-status-critical/8 px-1.5 py-px font-mono text-[11px] text-status-critical">{host.container_reason}</span> : null}
        </div>
        <div className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
          <ServerIcon className="size-3.5 shrink-0" aria-hidden="true" />
          <TruncatedText text={host.pod_name} className="max-w-[min(100%,36rem)] font-mono text-[11px]" />
        </div>
      </div>
      <MetaLine className="justify-end">
        <span title={host.stamp_id}>{stampName ? <>On <span className="font-medium text-foreground">{stampName}</span></> : <>Stamp <span className="font-mono">{shortId(host.stamp_id)}</span></>}</span>
        {host.started_at && !host.ready ? <span className="tabular-nums">Started {formatDuration(elapsedSeconds(host.started_at, now))} ago</span> : null}
        {host.restart_count ? <span className={cn("tabular-nums", host.restart_count >= 3 && "font-medium text-destructive")}>{host.restart_count} restart{host.restart_count === 1 ? "" : "s"}</span> : null}
        {host.stale ? <Status tone="warning" className="text-xs">No report {formatRelative(host.reported_at)}</Status> : <span>Reported {formatRelative(host.reported_at)}</span>}
      </MetaLine>
    </PanelHeader>
    <PanelContent>
      <div className="flex flex-col gap-4 border-b border-border p-5">
        <StageTimeline stages={stages} details={{ pulling: pullDetail, downloading: downloadDetail }} progress={weights?.fraction ?? null} />
        {diagnosis ? <Alert variant="destructive"><TriangleAlertIcon /><AlertTitle>{diagnosis.title}</AlertTitle><AlertDescription>{diagnosis.hint}</AlertDescription></Alert> : null}
        {!diagnosis && pull.state === "pulling" ? <Alert><InfoIcon /><AlertTitle>First deployment on this machine</AlertTitle><AlertDescription>The first deployment on a machine downloads the model runtime image once{pull.sizeBytes ? ` (${formatBytes(pull.sizeBytes)})` : " (several GB)"}. Later deployments on this machine start from the local copy and skip this step.</AlertDescription></Alert> : null}
        {!diagnosis && summary.detail && summary.tone !== "critical" ? <p className="flex items-start gap-2 text-[13px] text-muted-foreground"><InfoIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />{summary.detail}</p> : null}
      </div>
      <LogViewer host={host} />
      <EventList host={host} />
    </PanelContent>
  </Panel>
}

// The start-up stages as a stepper: a vertical list on narrow screens, one row on wide ones,
// each step a marker joined to the next by a rule that fills in as steps complete.
function StageTimeline({ stages, details, progress = null }: { stages: Stage[]; details: Partial<Record<Stage["key"], string>>; progress?: number | null }) {
  return <ol className="grid gap-y-0 xl:grid-cols-7" aria-label="Start-up stages">
    {stages.map((stage, index) => {
      const last = index === stages.length - 1
      const detail = details[stage.key] && stage.state !== "pending" ? details[stage.key] : null
      return <li key={stage.key} aria-current={stage.state === "current" ? "step" : undefined} className="relative flex min-w-0 gap-3 pb-4 xl:flex-col xl:gap-2 xl:pr-3 xl:pb-0">
        {!last ? <span aria-hidden="true" className={cn("absolute top-6 bottom-0 left-[9px] w-px xl:top-[9px] xl:right-0 xl:bottom-auto xl:left-6 xl:h-px xl:w-auto", stage.state === "done" ? "bg-foreground/30" : "bg-border")} /> : null}
        <StageMarker state={stage.state} />
        <div className="min-w-0 pt-px xl:pt-0">
          <div className={cn("text-[13px] leading-5",
            stage.state === "done" && "text-foreground/80",
            stage.state === "current" && "font-medium text-foreground",
            stage.state === "failed" && "font-medium text-destructive",
            stage.state === "pending" && "text-muted-foreground",
          )}>{stage.label}<span className="sr-only"> ({stage.state})</span></div>
          {detail ? <p className="mt-0.5 text-xs leading-4 text-muted-foreground tabular-nums">{detail}</p> : null}
          {stage.key === "downloading" && progress !== null && stage.state !== "pending" ? <Progress value={Math.round(progress * 100)} aria-label="Weight download progress" className="mt-2 max-w-48" /> : null}
        </div>
      </li>
    })}
  </ol>
}

function StageMarker({ state }: { state: Stage["state"] }) {
  return <span aria-hidden="true" className={cn("relative z-10 grid size-[19px] shrink-0 place-items-center rounded-full border bg-card",
    state === "done" && "border-foreground/70 bg-foreground/85 text-background",
    state === "current" && "border-info bg-info/10",
    state === "failed" && "border-destructive bg-destructive text-white",
    state === "pending" && "border-border",
  )}>
    {state === "done" ? <CheckIcon className="size-3" strokeWidth={3} /> : state === "failed" ? <XIcon className="size-3" strokeWidth={3} /> : state === "current" ? <span className="size-2 rounded-full bg-info motion-safe:animate-pulse" /> : null}
  </span>
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
    parts.push(<mark key={found} className="rounded-[3px] bg-brand-tint px-px text-foreground ring-1 ring-brand/30">{text.slice(found, found + needle.length)}</mark>)
    index = found + needle.length
  }
  return parts
}

const severityClass: Record<string, string> = {
  DEBUG: "text-muted-foreground/70",
  INFO: "text-muted-foreground",
  WARN: "font-medium text-status-warning",
  ERROR: "font-medium text-status-critical",
  CRITICAL: "font-medium text-status-critical",
  FATAL: "font-medium text-status-critical",
}

function LogViewer({ host }: { host: HostSnapshot }) {
  const pane = useRef<HTMLDivElement>(null)
  const followingRef = useRef(true)
  const [following, setFollowing] = useState(true)
  const [paused, setPaused] = useState<string | null>(null)
  const [query, setQuery] = useState("")
  const [level, setLevel] = useState<LogLevel>("all")
  const [wrap, setWrap] = useState(true)
  const text = paused ?? host.log_tail ?? ""
  const lines = useMemo(() => logLines(text, query, level), [text, query, level])
  const allLines = useMemo(() => logLines(text), [text])
  const problems = allLines.filter((line) => line.level !== "info").length
  const structured = allLines.some((line) => line.time !== null)
  const total = allLines.length
  const newSincePause = paused !== null && host.log_tail ? Math.max(0, host.log_tail.split("\n").length - total) : 0

  // New lines keep the view pinned to the bottom unless the reader scrolled up or paused.
  useLayoutEffect(() => {
    const element = pane.current
    if (element && followingRef.current) element.scrollTop = element.scrollHeight
  }, [lines, wrap])

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

  const columns = structured ? "grid-cols-[minmax(0,1fr)] sm:grid-cols-[2.5rem_7.25rem_3.25rem_minmax(0,1fr)]" : "grid-cols-[minmax(0,1fr)] sm:grid-cols-[2.5rem_minmax(0,1fr)]"
  const live = paused === null && following

  return <div className="flex flex-col gap-3 border-b border-border p-5">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h4 className="flex shrink-0 items-center gap-2 text-sm font-medium"><TerminalIcon className="size-4 text-muted-foreground" aria-hidden="true" />Server log</h4>
      {host.log_previous ? <span className="rounded-md bg-status-critical/8 px-2 py-0.5 text-xs text-status-critical">Previous instance, before the last restart</span> : null}
    </div>
    <div className="overflow-hidden rounded-md border border-border bg-muted/30">
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-card px-2.5 py-2">
        <div className="relative min-w-40 flex-1">
          <SearchIcon aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search the log" aria-label="Search the log" className="h-8 pl-8 text-[13px]" />
        </div>
        <Segmented label="Filter lines" value={level} onChange={setLevel} options={[
          { value: "all", label: "All", count: total },
          { value: "problems", label: "Warnings & errors", count: problems },
        ]} />
        <div className="flex items-center gap-1">
          <Button type="button" size="sm" variant={wrap ? "secondary" : "ghost"} aria-pressed={wrap} onClick={() => setWrap(!wrap)} title={wrap ? "Long lines wrap; turn off to scroll sideways" : "Long lines scroll sideways; turn on to wrap"}><WrapTextIcon />Wrap</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setPaused(paused === null ? host.log_tail ?? "" : null)} aria-pressed={paused !== null} disabled={!text}>{paused === null ? <><PauseIcon />Pause</> : <><PlayIcon />Resume{newSincePause ? <span className="tabular-nums"> ({newSincePause} new)</span> : null}</>}</Button>
          <Button type="button" size="sm" variant="ghost" onClick={download} disabled={!host.log_tail}><DownloadIcon />Download</Button>
        </div>
      </div>
      {text ? <div ref={pane} onScroll={onScroll} tabIndex={0} role="log" aria-label={`Log of ${host.pod_name}`} className="max-h-[min(62vh,40rem)] min-h-40 overflow-auto font-mono text-xs leading-5 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
        <div className={cn("min-w-full pb-1", !wrap && "w-max")}>
          <div aria-hidden="true" className={cn("sticky top-0 z-10 mb-1 hidden border-b border-border bg-card px-3 py-1 font-sans text-[11px] font-medium tracking-wide text-muted-foreground uppercase sm:grid", columns)}>
            <span className="pr-3 text-right">#</span>{structured ? <><span>Time</span><span>Level</span></> : null}<span>Message</span>
          </div>
          {lines.length ? lines.map((line) => <LogRow key={line.number} line={line} query={query} wrap={wrap} structured={structured} columns={columns} />)
            : <p className="px-4 py-6 text-center font-sans text-[13px] text-muted-foreground">No lines match{query ? <> &ldquo;{query}&rdquo;</> : null}{level === "problems" ? " among warnings and errors" : ""}.</p>}
        </div>
      </div> : <Empty className="min-h-40 rounded-none border-0 py-8">
        <EmptyHeader>
          <EmptyMedia variant="icon"><TerminalIcon /></EmptyMedia>
          <EmptyTitle>No log output yet</EmptyTitle>
          <EmptyDescription>{host.log_error ? <>The stamp says: {host.log_error.replace(/\.$/, "")}. The server&apos;s output appears here once its container starts.</> : "The server's output appears here once its container starts."}</EmptyDescription>
        </EmptyHeader>
      </Empty>}
      <div className="flex min-h-9 flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-border bg-card px-3 py-1.5">
        <MetaLine>
          {total ? <span className="tabular-nums">{query || level !== "all" ? `${lines.length} of ${total}` : total} line{total === 1 ? "" : "s"}</span> : <span>Nothing received</span>}
          {total ? <span>The last 200 the stamp sent</span> : null}
          {host.log_truncated ? <span>Long lines trimmed</span> : null}
        </MetaLine>
        <div className="flex items-center gap-2">
          {text ? <Status tone={live ? "good" : "neutral"} pulse={live} className="text-xs text-muted-foreground">{paused !== null ? "Paused" : following ? "Following" : "Scrolled up"}</Status> : null}
          {text && !live ? <Button type="button" variant="outline" size="xs" onClick={jump}><ArrowDownToLineIcon />Jump to latest</Button> : null}
        </div>
      </div>
    </div>
  </div>
}

function LogRow({ line, query, wrap, structured, columns }: { line: LogLine; query: string; wrap: boolean; structured: boolean; columns: string }) {
  return <div className={cn("grid px-3 hover:bg-muted/70", columns,
    line.level === "error" && "bg-status-critical/6 shadow-[inset_2px_0_0_var(--status-critical)]",
    line.level === "warning" && "bg-status-warning/7 shadow-[inset_2px_0_0_var(--status-warning)]",
  )}>
    <span aria-hidden="true" className="hidden pr-3 text-right text-muted-foreground/60 select-none tabular-nums sm:block">{line.number}</span>
    {structured ? <>
      <span className="hidden whitespace-nowrap text-muted-foreground tabular-nums sm:block">{line.time ?? ""}</span>
      <span className={cn("hidden sm:block", line.severity ? severityClass[line.severity] ?? "text-muted-foreground" : "")}>{line.severity ?? ""}</span>
    </> : null}
    <span className={cn("min-w-0", wrap ? "break-words whitespace-pre-wrap [overflow-wrap:anywhere]" : "whitespace-pre")}>
      {structured && line.time ? <span className="mr-2 text-muted-foreground tabular-nums sm:hidden">{line.time.slice(6)}</span> : null}
      {line.severity && line.severity !== "INFO" ? <span className={cn("mr-2 sm:hidden", severityClass[line.severity])}>{line.severity}</span> : null}
      {line.source ? <span className="mr-2 text-muted-foreground/80">[{highlight(line.source, query)}]</span> : null}
      {highlight(structured && line.time ? line.message : line.text, query)}
    </span>
  </div>
}

function EventList({ host }: { host: HostSnapshot }) {
  const events = [...host.events].reverse()
  const failures = events.filter(isFailureEvent).length
  return <details className="group" open={failures > 0}>
    <summary className="flex cursor-pointer list-none items-center gap-2 px-5 py-3.5 text-sm outline-none select-none hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset [&::-webkit-details-marker]:hidden">
      <ChevronRightIcon className="size-4 text-muted-foreground transition-transform duration-(--motion-fast) group-open:rotate-90" aria-hidden="true" />
      <span className="font-medium">Kubernetes events</span>
      <span className="rounded-full bg-muted px-2 py-px text-xs text-muted-foreground tabular-nums">{events.length}</span>
      {failures ? <span className="rounded-full bg-status-critical/10 px-2 py-px text-xs text-status-critical tabular-nums">{failures} warning{failures === 1 ? "" : "s"}</span> : null}
      <span className="ml-auto hidden text-xs text-muted-foreground sm:inline">Kept by Kubernetes for about an hour</span>
    </summary>
    {events.length ? <ul className="max-h-80 overflow-auto border-t border-border">
      {events.map((event, index) => {
        const failure = isFailureEvent(event)
        return <li key={`${event.at}-${event.reason}-${index}`} className="grid gap-x-4 gap-y-0.5 border-b border-border/60 px-5 py-2.5 text-[13px] last:border-b-0 sm:grid-cols-[8rem_10rem_minmax(0,1fr)]">
          <time className="text-xs leading-5 text-muted-foreground tabular-nums" dateTime={event.at}>{formatRelative(event.at)}</time>
          <span className={cn("flex h-5 min-w-0 items-center gap-1.5 self-start font-medium", failure && "text-destructive")}>
            <Status tone={failure ? "critical" : isStartupWait(event) ? "info" : "neutral"} className="min-w-0 text-[13px]"><span className="truncate">{event.reason}</span></Status>
            {event.count > 1 ? <span className="text-xs font-normal text-muted-foreground tabular-nums">×{event.count}</span> : null}
          </span>
          <div className="min-w-0">
            <p className={cn("break-words", failure ? "text-destructive" : "text-muted-foreground")}>{event.message}</p>
            {isStartupWait(event) ? <p className="mt-0.5 text-xs text-muted-foreground/80">Expected while the model starts: the server opens its port once the weights are loaded.</p> : null}
          </div>
        </li>
      })}
    </ul> : <p className="border-t border-border px-5 py-4 text-[13px] text-muted-foreground">No recent events. Kubernetes keeps events for about an hour.</p>}
  </details>
}
