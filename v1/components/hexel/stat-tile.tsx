import * as React from "react"
import { cn } from "cn"

import { Sparkline } from "@/components/hexel/sparkline"

/**
 * A row of headline numbers in one card (16px `radius-surface` corners, a
 * 1px ring), split by hairlines rather than floated as separate cards. The
 * hairlines are the card's own background showing through a 1px gap, so they
 * stay exact at every breakpoint.
 */
function StatGroup({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="stat-group"
      className={cn(
        "grid grid-cols-1 gap-px overflow-hidden rounded-[var(--radius-surface)] bg-border ring-1 ring-border sm:grid-cols-2 lg:grid-cols-4",
        className
      )}
      {...props}
    />
  )
}

type Delta = {
  /** Signed change, already formatted ("+12%", "−38 ms"). */
  value: string
  /** Which way it moved. */
  direction: "up" | "down" | "flat"
  /** Whether that movement is good news. Colours the delta; the arrow carries the direction. */
  good?: boolean
}

/**
 * One headline number: label, value, optional unit, delta against a named
 * period and a 12-point trend. Values use proportional figures (they stand
 * alone); the delta is coloured by whether the change is good, never by its
 * direction alone.
 */
function StatTile({
  label,
  value,
  unit,
  delta,
  period,
  trend,
  className,
  ...props
}: Omit<React.ComponentProps<"div">, "children"> & {
  label: React.ReactNode
  value: React.ReactNode
  unit?: React.ReactNode
  delta?: Delta
  period?: React.ReactNode
  trend?: number[]
}) {
  const tone =
    !delta || delta.direction === "flat" || delta.good === undefined
      ? "text-muted-foreground"
      : delta.good
        ? "text-success"
        : "text-destructive"
  return (
    <div
      data-slot="stat-tile"
      className={cn("flex min-w-0 flex-col gap-3 bg-card p-4", className)}
      {...props}
    >
      <div className="text-[13px] text-muted-foreground">{label}</div>
      <div className="flex items-end justify-between gap-3">
        <div className="flex min-w-0 items-baseline gap-1">
          <span className="text-2xl leading-none font-semibold tracking-[-0.02em] whitespace-nowrap">
            {value}
          </span>
          {unit && (
            <span className="text-sm text-muted-foreground">{unit}</span>
          )}
        </div>
        {trend && trend.length > 1 && (
          <Sparkline data={trend} width={84} height={26} />
        )}
      </div>
      {(delta || period) && (
        <div className="flex items-center gap-1.5 text-xs">
          {delta && (
            <span
              className={cn(
                "inline-flex items-center gap-0.5 font-medium whitespace-nowrap tabular-nums",
                tone
              )}
            >
              {delta.direction !== "flat" && (
                <svg
                  viewBox="0 0 12 12"
                  aria-hidden="true"
                  className={cn(
                    "size-3",
                    delta.direction === "down" && "rotate-180"
                  )}
                >
                  <path
                    d="M6 9.5V2.5M2.75 5.75 6 2.5l3.25 3.25"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              )}
              {delta.value}
            </span>
          )}
          {period && <span className="text-muted-foreground">{period}</span>}
        </div>
      )}
    </div>
  )
}

export { StatGroup, StatTile }
