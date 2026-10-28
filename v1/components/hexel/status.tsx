import * as React from "react"
import { cn } from "cn"

type Tone =
  "good" | "warning" | "serious" | "critical" | "info" | "brand" | "neutral"

const dot: Record<Tone, string> = {
  good: "bg-status-good",
  warning: "bg-status-warning",
  serious: "bg-status-serious",
  critical: "bg-status-critical",
  info: "bg-info",
  brand: "bg-foreground",
  neutral: "bg-muted-foreground/55",
}

/**
 * One way to show state in the consoles: a 6px dot in a status colour beside
 * a plain label. The label stays in text colour, so the dot is never the only
 * cue and dense tables stay calm. `pulse` adds a soft ping for live states
 * (connected, streaming); it stops under reduced motion.
 */
function Status({
  tone = "neutral",
  pulse = false,
  className,
  children,
  ...props
}: React.ComponentProps<"span"> & { tone?: Tone; pulse?: boolean }) {
  return (
    <span
      data-slot="status"
      data-tone={tone}
      className={cn(
        "inline-flex items-center gap-2 text-sm whitespace-nowrap",
        className
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className="relative grid size-1.5 shrink-0 place-items-center"
      >
        {pulse && (
          <span
            className={cn(
              "absolute inset-0 rounded-full opacity-60 motion-safe:animate-ping-soft",
              dot[tone]
            )}
          />
        )}
        <span className={cn("size-1.5 rounded-full", dot[tone])} />
      </span>
      {children}
    </span>
  )
}

export { Status }
export type { Tone as StatusTone }
