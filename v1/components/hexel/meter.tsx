import * as React from "react"
import { cn } from "cn"

/**
 * A single ratio against a limit: budget spent, quota used, CPU. The fill
 * carries severity (the chart blue, then `status-warning` from `warn`, then
 * `status-critical` from `critical`); the track is a light step of the same
 * colour, so the state reads across the whole bar. The numbers are always
 * printed beside it.
 */
function Meter({
  value,
  max = 100,
  warn = 0.75,
  critical = 0.9,
  label,
  detail,
  size = "default",
  className,
  ...props
}: Omit<React.ComponentProps<"div">, "children"> & {
  value: number
  max?: number
  warn?: number
  critical?: number
  label?: React.ReactNode
  detail?: React.ReactNode
  size?: "sm" | "default"
}) {
  const ratio = max > 0 ? Math.min(Math.max(value / max, 0), 1) : 0
  const fill =
    ratio >= critical
      ? "var(--status-critical)"
      : ratio >= warn
        ? "var(--status-warning)"
        : "var(--chart-accent)"
  return (
    <div
      data-slot="meter"
      className={cn("flex min-w-0 flex-col gap-1.5", className)}
      {...props}
    >
      {(label || detail) && (
        <div className="flex items-baseline justify-between gap-3 text-[13px]">
          {label && <span className="truncate">{label}</span>}
          {detail && (
            <span className="shrink-0 text-muted-foreground tabular-nums">
              {detail}
            </span>
          )}
        </div>
      )}
      <div
        role="meter"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
        className={cn(
          "w-full overflow-hidden rounded-full",
          size === "sm" ? "h-1" : "h-1.5"
        )}
        style={{ background: `color-mix(in oklab, ${fill} 16%, var(--card))` }}
      >
        <div
          className="h-full rounded-full transition-[width] duration-(--motion-slow) ease-sharp"
          style={{
            width: `${ratio * 100}%`,
            background: `linear-gradient(90deg, color-mix(in oklab, ${fill} 70%, var(--card)), ${fill})`,
          }}
        />
      </div>
    </div>
  )
}

export { Meter }
