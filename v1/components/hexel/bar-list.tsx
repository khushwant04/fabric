import * as React from "react"
import { cn } from "cn"

type BarListItem = {
  key: string
  label: React.ReactNode
  value: number
  href?: string
  meta?: React.ReactNode
}

/**
 * A ranked list: the busiest APIs, top tools, largest meters. Each row's bar
 * runs behind its label as a quiet neutral wash, scaled to the largest value,
 * and stops short of the value column, so the list reads as bars and never
 * as a selected row. One series, so one colour; rank is carried by order and
 * length. Values are in tabular figures.
 */
function BarList({
  items,
  valueFormatter = (v) => v.toLocaleString(),
  className,
  ...props
}: Omit<React.ComponentProps<"ol">, "children"> & {
  items: BarListItem[]
  valueFormatter?: (value: number) => string
}) {
  const max = Math.max(1, ...items.map((i) => i.value))
  return (
    <ol
      data-slot="bar-list"
      className={cn("flex flex-col gap-1.5", className)}
      {...props}
    >
      {items.map((item) => {
        const body = (
          <>
            <span className="relative flex h-7 min-w-0 flex-1 items-center">
              <span
                aria-hidden="true"
                className="absolute inset-y-0 left-0 rounded-md bg-[linear-gradient(90deg,color-mix(in_oklab,var(--foreground)_4%,transparent),color-mix(in_oklab,var(--foreground)_11%,transparent))] transition-[width] duration-(--motion-slow) ease-sharp"
                style={{ width: `${Math.max((item.value / max) * 100, 1.5)}%` }}
              />
              <span className="relative flex min-w-0 items-baseline gap-2 px-2">
                <span className="truncate">{item.label}</span>
                {item.meta && (
                  <span className="shrink-0 truncate text-xs text-muted-foreground">
                    {item.meta}
                  </span>
                )}
              </span>
            </span>
            <span className="w-16 shrink-0 text-right font-mono text-[13px] tabular-nums">
              {valueFormatter(item.value)}
            </span>
          </>
        )
        const row = "flex items-center gap-3 text-sm"
        return (
          <li key={item.key}>
            {item.href ? (
              <a
                href={item.href}
                className={cn(
                  row,
                  "rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring hover:[&>span:first-child>span:first-child]:bg-[linear-gradient(90deg,color-mix(in_oklab,var(--foreground)_7%,transparent),color-mix(in_oklab,var(--foreground)_16%,transparent))]"
                )}
              >
                {body}
              </a>
            ) : (
              <div className={row}>{body}</div>
            )}
          </li>
        )
      })}
    </ol>
  )
}

export { BarList }
export type { BarListItem }
