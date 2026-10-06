"use client"

import { startTransition, useEffect, useRef, useState } from "react"
import { RefreshCwIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { createLiveRefresh } from "@/lib/fabric/live-refresh"
import { refreshConsoleView } from "@/lib/fabric/refresh-actions"

export function LiveRefresh({ checkedAt, error, intervalMs = 8000 }: {
  checkedAt: string
  error?: string | null
  intervalMs?: number
}) {
  const [pending, setPending] = useState(false)
  const [requestError, setRequestError] = useState<string | null>(null)
  const control = useRef<ReturnType<typeof createLiveRefresh> | null>(null)
  const lastCheckedAt = useRef(checkedAt)
  useEffect(() => { lastCheckedAt.current = checkedAt }, [checkedAt])
  useEffect(() => {
    let mounted = true
    const controller = createLiveRefresh({
      intervalMs,
      isVisible: () => document.visibilityState === "visible",
      refresh: () => {
        if (mounted) setPending(true)
        return new Promise<void>((resolve) => {
          startTransition(async () => {
            try {
              const result = await refreshConsoleView()
              if (mounted) setRequestError(result.error ?? null)
            } catch {
              if (mounted) setRequestError("Refresh failed. Check your connection and try again.")
            } finally {
              if (mounted) setPending(false)
              resolve()
            }
          })
        })
      },
    })
    control.current = controller
    const visible = () => { if (document.visibilityState === "visible") void controller.run() }
    document.addEventListener("visibilitychange", visible)
    // A prefetched or revisited page may already be older than one poll interval.
    // Keep its content visible and check it immediately instead of waiting again.
    const age = Date.now() - Date.parse(lastCheckedAt.current)
    if (!Number.isFinite(age) || age >= Math.max(5000, Math.min(intervalMs, 30_000))) {
      void controller.run()
    } else controller.start()
    return () => { mounted = false; controller.stop(); control.current = null; document.removeEventListener("visibilitychange", visible) }
  }, [intervalMs])
  const failure = requestError || error
  return <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" aria-live="polite">
    <span>{failure ? <span className="text-destructive">{failure} Displayed data may be outdated.</span> : <>Last checked <time dateTime={checkedAt}>{checkedAt.slice(11, 19)} UTC</time> · auto refresh every {Math.max(5, Math.min(intervalMs / 1000, 30))}s</>}</span>
    <Button type="button" variant="ghost" size="sm" disabled={pending} onClick={() => { void control.current?.run(true) }}><RefreshCwIcon className={pending ? "animate-spin" : ""} />{pending ? "Refreshing…" : "Refresh"}</Button>
  </div>
}
