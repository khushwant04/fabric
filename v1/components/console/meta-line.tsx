import { Children, type ComponentProps, type ReactNode } from "react"

import { cn } from "@/lib/utils"

/**
 * A row of small muted facts split by middle dots, e.g. "Last report 5s ago · sources vLLM ·
 * refreshing every 15s". Empty items are skipped, so callers can pass conditionals inline.
 * It wraps between items, never inside one.
 */
export function MetaLine({ className, children, ...props }: ComponentProps<"div"> & { children: ReactNode }) {
  const items = Children.toArray(children).filter((item) => item !== "")
  return <div className={cn("flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground", className)} {...props}>
    {items.map((item, index) => <span key={index} className="inline-flex items-center gap-2 whitespace-nowrap">
      {index > 0 ? <span aria-hidden="true" className="text-muted-foreground/50">·</span> : null}
      {item}
    </span>)}
  </div>
}
