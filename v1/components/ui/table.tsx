"use client"

import * as React from "react"
import { cn } from "cn"

/**
 * A table that scrolls sideways inside its card when its columns do not fit. Edge fades
 * appear on whichever side has more columns, the scrollbar stays visible, and the scroll
 * area becomes focusable so it can be scrolled from the keyboard.
 */
function Table({ className, containerClassName, ...props }: React.ComponentProps<"table"> & { containerClassName?: string }) {
  const containerRef = React.useRef<HTMLDivElement>(null)
  const [edges, setEdges] = React.useState({ start: false, end: false })

  React.useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const update = () => {
      const start = container.scrollLeft > 1
      const end = container.scrollLeft + container.clientWidth < container.scrollWidth - 1
      setEdges((current) => current.start === start && current.end === end ? current : { start, end })
    }
    // The observer reports once on observe, so the first measurement needs no direct call.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update)
    observer?.observe(container)
    if (container.firstElementChild) observer?.observe(container.firstElementChild)
    container.addEventListener("scroll", update, { passive: true })
    return () => {
      observer?.disconnect()
      container.removeEventListener("scroll", update)
    }
  }, [])

  const scrollable = edges.start || edges.end
  return (
    <div data-slot="table-frame" className="relative w-full min-w-0">
      <div
        ref={containerRef}
        data-slot="table-container"
        data-scrollable={scrollable ? "" : undefined}
        tabIndex={scrollable ? 0 : undefined}
        className={cn(
          "relative w-full overflow-x-auto outline-none [scrollbar-color:var(--border)_transparent] [scrollbar-width:thin] focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-inset",
          containerClassName
        )}
      >
        <table
          data-slot="table"
          className={cn("w-full caption-bottom text-sm", className)}
          {...props}
        />
      </div>
      <div aria-hidden="true" data-slot="table-fade" data-side="start" data-visible={edges.start ? "" : undefined} className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-linear-to-r from-card to-transparent opacity-0 transition-opacity duration-200 data-visible:opacity-100" />
      <div aria-hidden="true" data-slot="table-fade" data-side="end" data-visible={edges.end ? "" : undefined} className="pointer-events-none absolute inset-y-0 right-0 w-10 bg-linear-to-l from-card via-card/70 to-transparent opacity-0 transition-opacity duration-200 data-visible:opacity-100" />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b", className)}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b transition-colors hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted",
        className
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-10 px-2 text-left align-middle font-medium whitespace-nowrap text-foreground [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "p-2 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
