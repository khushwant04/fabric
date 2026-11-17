"use client"

import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

/** The muted track and raised selected segment shared by every console segmented control. */
export const segmentedTrackClass = "inline-flex max-w-full flex-wrap gap-0.5 rounded-lg bg-muted p-0.5"
export const segmentClass = "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[13px] whitespace-nowrap text-muted-foreground transition-colors duration-(--motion-fast) outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
export const segmentSelectedClass = "bg-card font-medium text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.08)] dark:bg-background"

export type SegmentOption<T extends string> = { value: T; label: ReactNode; ariaLabel?: string; count?: number }

/**
 * A small set of mutually exclusive buttons, e.g. a time window or a line filter. Each segment
 * is a toggle button (aria-pressed) inside a labelled group.
 */
export function Segmented<T extends string>({ label, value, options, onChange, className }: {
  label: string
  value: T
  options: ReadonlyArray<SegmentOption<T>>
  onChange: (value: T) => void
  className?: string
}) {
  return <div role="group" aria-label={label} className={cn(segmentedTrackClass, className)}>
    {options.map((option) => <button key={option.value} type="button" aria-pressed={value === option.value} aria-label={option.ariaLabel} onClick={() => onChange(option.value)}
      className={cn(segmentClass, value === option.value && segmentSelectedClass)}>
      {option.label}
      {option.count !== undefined ? <span className="tabular-nums text-muted-foreground">{option.count}</span> : null}
    </button>)}
  </div>
}
