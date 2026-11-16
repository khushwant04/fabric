"use client"

import { useEffect, useState } from "react"

import { metricsPollDelay, type MetricWindow } from "@/lib/fabric/serving-metrics"

// Fetches a metrics route handler for the selected window and refetches on the window's
// cadence while the browser tab is visible, immediately when it becomes visible again.
export function useMetrics<T>(path: string, window: MetricWindow, enabled = true) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadedWindow, setLoadedWindow] = useState<MetricWindow | null>(null)

  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const delay = metricsPollDelay(window)
    async function poll() {
      if (document.visibilityState === "visible") {
        try {
          const response = await fetch(`${path}?window=${window}`, { cache: "no-store", signal: controller.signal })
          const body = await response.json().catch(() => null)
          if (stopped) return
          if (response.ok) { setData(body as T); setError(null); setLoadedWindow(window) }
          else setError((body as { error?: { message?: string } } | null)?.error?.message ?? "Metrics could not be read.")
        } catch {
          if (stopped) return
          setError("Metrics could not be read. Check your connection.")
        } finally {
          if (!stopped) setLoading(false)
        }
      }
      if (!stopped) timer = setTimeout(poll, delay)
    }
    void poll()
    const visible = () => { if (document.visibilityState === "visible") { if (timer) clearTimeout(timer); void poll() } }
    document.addEventListener("visibilitychange", visible)
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer); document.removeEventListener("visibilitychange", visible) }
  }, [path, window, enabled])

  return { data, error, loading: loading || loadedWindow !== window && data === null, switching: loadedWindow !== null && loadedWindow !== window }
}
