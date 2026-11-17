import { Children, Fragment, isValidElement, type ComponentProps, type ReactElement, type ReactNode } from "react"

import { cn } from "@/lib/utils"

function flatten(children: ReactNode): ReactNode[] {
  return Children.toArray(children).flatMap((child) => isValidElement(child) && child.type === Fragment
    ? flatten((child as ReactElement<{ children?: ReactNode }>).props.children)
    : child === "" ? [] : [child])
}

/**
 * A row of small muted facts split by middle dots, e.g. "Last report 5s ago · Sources: vLLM ·
 * Refreshing every 15s". Empty items are skipped and fragments are spread, so callers can pass
 * conditionals inline. It wraps between items, never inside one.
 */
export function MetaLine({ className, children, ...props }: ComponentProps<"div"> & { children: ReactNode }) {
  const items = flatten(children)
  return <div className={cn("flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground", className)} {...props}>
    {items.map((item, index) => <span key={index} className="inline-flex items-center gap-2 whitespace-nowrap">
      {index > 0 ? <span aria-hidden="true" className="text-muted-foreground/50">·</span> : null}
      {item}
    </span>)}
  </div>
}
