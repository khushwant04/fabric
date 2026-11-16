"use client"

import { useEffect, useState } from "react"
import { ActivityIcon } from "lucide-react"

import { Status } from "@/components/hexel/status"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { activityHeadline, activityLabel, activityPollDelay, activityTone, sortActivity } from "@/lib/fabric/stamp-activity"
import type { StampEvent, StampEvents } from "@/lib/fabric/types"
import { formatDate, formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"

// A stamp's live registration and activity log: what its agent recorded (enrolled, GPUs
// measured, GPU runtime, rotations, generations applied, image pulls, failures) and what the
// control plane did (removal). Newest first; polls a console route handler while the page is
// open and visible.
export function StampActivity({ stampId, initial, initialError }: {
  stampId: string
  initial: StampEvents | null
  initialError?: string | null
}) {
  const [data, setData] = useState<StampEvents | null>(initial)
  const [error, setError] = useState<string | null>(initialError ?? null)
  // Re-renders relative times between polls.
  const [, setTick] = useState(0)
  const delay = activityPollDelay(data)

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    async function poll() {
      if (document.visibilityState === "visible") {
        try {
          const response = await fetch(`/api/stamps/${stampId}/events`, { cache: "no-store", signal: controller.signal })
          const body = await response.json().catch(() => null)
          if (stopped) return
          if (response.ok) { setData(body as StampEvents); setError(null) }
          else setError((body as { error?: { message?: string } } | null)?.error?.message ?? "Activity could not be read.")
        } catch {
          if (stopped) return
          setError("Activity could not be read. Check your connection.")
        }
        setTick((value) => value + 1)
      }
      if (!stopped) timer = setTimeout(poll, delay)
    }
    timer = setTimeout(poll, delay)
    const visible = () => { if (document.visibilityState === "visible") { if (timer) clearTimeout(timer); void poll() } }
    document.addEventListener("visibilitychange", visible)
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer); document.removeEventListener("visibilitychange", visible) }
  }, [stampId, delay])

  const events = sortActivity(data?.events ?? [])
  const headline = activityHeadline(events)

  return <div className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" aria-live="polite">
      {error ? <span className="text-destructive">{error} Showing the last read.</span> : <Status tone={data?.revoked_at ? "neutral" : "info"} pulse={!data?.revoked_at} className="text-xs">{data?.revoked_at ? "Stamp removed · log kept" : `Live · checking every ${Math.round(delay / 1000)}s`}</Status>}
      <span>{events.length ? `${events.length} event${events.length === 1 ? "" : "s"} · newest first` : null}</span>
    </div>
    {headline ? <p className="border bg-muted/30 px-3 py-2 text-sm" data-testid="activity-headline">{headline}</p> : null}
    {events.length ? <ol className="relative" aria-label="Stamp activity, newest first">
      {events.map((event, index) => <ActivityItem key={event.id} event={event} last={index === events.length - 1} />)}
    </ol> : <Alert><ActivityIcon /><AlertTitle>No activity yet</AlertTitle><AlertDescription>Events appear here as soon as the stamp&apos;s agent enrolls: the GPUs it found, whether the GPU runtime works, and when it is ready for deployments. Agents older than this console do not report activity.</AlertDescription></Alert>}
  </div>
}

const DOT: Record<string, string> = {
  good: "bg-status-good",
  info: "bg-info",
  warning: "bg-status-warning",
  critical: "bg-status-critical",
  neutral: "bg-muted-foreground/55",
}

function ActivityItem({ event, last }: { event: StampEvent; last: boolean }) {
  const tone = activityTone(event.severity)
  return <li className="relative grid grid-cols-[1rem_minmax(0,1fr)] gap-x-3 pb-4">
    <span aria-hidden="true" className="relative flex justify-center">
      <span className={cn("mt-1.5 size-2.5 shrink-0", DOT[tone])} />
      {last ? null : <span className="absolute top-5 bottom-[-0.25rem] w-px bg-border" />}
    </span>
    <div className="min-w-0">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className={cn("text-sm font-medium break-words", tone === "critical" && "text-destructive")}>{event.message}</p>
        <time className="shrink-0 text-xs tabular-nums text-muted-foreground" dateTime={event.occurred_at} title={formatDate(event.occurred_at)}>{formatRelative(event.occurred_at)}</time>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        <Badge variant="outline">{activityLabel(event.kind)}</Badge>
        {event.source === "control_plane" ? <Badge variant="secondary">Control plane</Badge> : null}
        <span className="sr-only">Severity: {event.severity}</span>
      </div>
      {event.hint ? <p className={cn("mt-1.5 text-xs break-words", tone === "critical" || tone === "warning" ? "text-foreground/80" : "text-muted-foreground")}>{event.hint}</p> : null}
    </div>
  </li>
}
