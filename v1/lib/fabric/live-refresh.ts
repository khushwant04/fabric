export function createLiveRefresh({
  refresh,
  isVisible,
  schedule = (callback: () => void, delay: number) => setTimeout(callback, delay),
  cancel = (timer: ReturnType<typeof setTimeout>) => clearTimeout(timer),
  intervalMs = 8000,
}: {
  refresh: () => Promise<void>
  isVisible: () => boolean
  schedule?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>
  cancel?: (timer: ReturnType<typeof setTimeout>) => void
  intervalMs?: number
}) {
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let flight: Promise<void> | undefined
  const delay = Math.max(5000, Math.min(intervalMs, 30_000))
  function later() {
    if (!stopped) timer = schedule(() => { void run() }, delay)
  }
  function run(force = false): Promise<void> {
    if (stopped) return Promise.resolve()
    if (flight) return flight
    if (timer !== undefined) { cancel(timer); timer = undefined }
    if (!force && !isVisible()) { later(); return Promise.resolve() }
    flight = Promise.resolve().then(refresh).finally(() => { flight = undefined; later() })
    return flight
  }
  return {
    run,
    start: later,
    stop() { stopped = true; if (timer !== undefined) cancel(timer) },
  }
}
