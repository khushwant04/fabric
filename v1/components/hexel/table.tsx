import * as React from "react"
import { cn } from "cn"

type Density = "compact" | "default" | "comfortable"

/**
 * Tables in the consoles are instruments, not cards: no outer border and no
 * rounded frame, only a 1px rule under a quiet header, hairlines between rows
 * and numbers in tabular figures on the right. They sit straight on the page
 * or inside a Card or ChartCard. `density` sets the row height (32, 40 or
 * 48px); `stickyHeader` pins the header while the table scrolls inside a
 * fixed-height parent, painted in the colour of the surface underneath
 * (`--table-surface`: the page, or the card it sits in).
 */
function Table({
  className,
  density = "default",
  stickyHeader = false,
  containerClassName,
  containerStyle,
  ...props
}: React.ComponentProps<"table"> & {
  density?: Density
  stickyHeader?: boolean
  containerClassName?: string
  containerStyle?: React.CSSProperties
}) {
  return (
    <div
      data-slot="table-container"
      data-density={density}
      data-sticky={stickyHeader || undefined}
      className={cn(
        "group/table relative w-full overflow-auto",
        "[--table-surface:var(--background)] in-data-[slot=card]:[--table-surface:var(--card)] in-data-[slot=chart-card]:[--table-surface:var(--card)] in-data-[slot=panel-content]:[--table-surface:var(--card)]",
        containerClassName
      )}
      style={containerStyle}
    >
      <table
        data-slot="table"
        className={cn(
          "w-full caption-bottom border-separate border-spacing-0 text-sm",
          className
        )}
        {...props}
      />
    </div>
  )
}

/** Transparent, with a 1px rule underneath. Sticky headers take the surface colour so rows pass under them cleanly. */
function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn(
        "group-data-[sticky]/table:sticky group-data-[sticky]/table:top-0 group-data-[sticky]/table:z-20 group-data-[sticky]/table:bg-(--table-surface)",
        className
      )}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&>tr:last-child>td]:border-b-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "font-medium [&>tr>td]:border-t [&>tr>td]:border-b-0",
        className
      )}
      {...props}
    />
  )
}

/** Hover is an instant wash; a selected row takes a deeper neutral tint. */
function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "[tbody>&]:hover:bg-muted/45",
        "data-[state=selected]:bg-muted/70 data-[state=selected]:hover:bg-muted",
        className
      )}
      {...props}
    />
  )
}

type Align = "left" | "right" | "center"
const align = (a?: Align) =>
  a === "right"
    ? "text-right [&>*]:ml-auto"
    : a === "center"
      ? "text-center"
      : "text-left"

function TableHead({
  className,
  align: a,
  ...props
}: React.ComponentProps<"th"> & { align?: Align }) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-9 border-b px-3 align-middle text-xs font-medium whitespace-nowrap text-muted-foreground first:pl-4 last:pr-4",
        "group-data-[density=comfortable]/table:h-10 group-data-[density=compact]/table:h-8",
        a === "right" && "tabular-nums",
        align(a),
        className
      )}
      {...props}
    />
  )
}

function TableCell({
  className,
  align: a,
  numeric,
  ...props
}: React.ComponentProps<"td"> & { align?: Align; numeric?: boolean }) {
  const right = numeric ? "right" : a
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "h-10 border-b px-3 align-middle whitespace-nowrap first:pl-4 last:pr-4",
        "group-data-[density=comfortable]/table:h-12 group-data-[density=compact]/table:h-8 group-data-[density=compact]/table:text-[13px]",
        numeric && "font-mono text-[13px] tabular-nums",
        align(right),
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
      className={cn("mt-3 text-left text-xs text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableRow,
  TableHead,
  TableCell,
  TableCaption,
}
export type { Density as TableDensity }
