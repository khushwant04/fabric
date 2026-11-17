import Link from "next/link"
import type { ComponentProps, ReactNode } from "react"
import { ArrowRightIcon } from "lucide-react"

import { cn } from "@/lib/utils"

// The console's building blocks, shared by every page: white rounded-2xl cards with a very
// light border, a 18px heading and a muted line under it; headline numbers as their own
// small cards; label/value rows split by hairlines.

/** The small uppercase label above a group of cards, e.g. "QUICK START". */
export const labelClass = "text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase"

/** A card: heading, optional description and action, then its content. */
export function Section({ title, description, action, className, children, ...props }: Omit<ComponentProps<"section">, "title"> & { title: ReactNode; description?: ReactNode; action?: ReactNode }) {
  return <section className={cn("flex min-w-0 flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[0_1px_2px_rgb(0_0_0/0.025)] [&>[data-slot=empty]]:rounded-none [&>[data-slot=empty]]:border-0 [&>[data-slot=empty]]:border-t [&>[data-slot=empty]]:border-solid [&>[data-slot=table-frame]]:border-t", className)} {...props}>
    <header className="flex shrink-0 flex-wrap items-start justify-between gap-x-4 gap-y-2 px-6 pt-5 pb-4">
      <div className="min-w-0">
        <h2 className="flex min-w-0 items-center gap-2 text-lg leading-7 font-medium tracking-[-0.01em]">{title}</h2>
        {description ? <p className="mt-0.5 max-w-3xl text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
    </header>
    {children}
  </section>
}

/** Padded content inside a Section, under its heading. */
export function SectionBody({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex min-w-0 flex-col gap-4 px-6 pb-6 text-sm", className)} {...props} />
}

/** A strip under a Section's content: pagination, a note, secondary actions. */
export function SectionFooter({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex min-h-14 items-center justify-between gap-2 border-t border-border px-6 py-3 text-sm text-muted-foreground", className)} {...props} />
}

/** An underlined text link with an arrow, e.g. "All deployments →". */
export function SectionLink({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  return <Link href={href} className={cn("group/link inline-flex items-center gap-1 text-sm font-medium text-foreground underline decoration-foreground/25 underline-offset-4 outline-none hover:decoration-foreground focus-visible:ring-2 focus-visible:ring-ring rounded-sm", className)}>
    {children}<ArrowRightIcon className="size-3.5 transition-transform group-hover/link:translate-x-0.5" />
  </Link>
}

/** A centred muted sentence for an empty or unavailable Section, with an optional action. */
export function SectionEmpty({ className, action, children, ...props }: ComponentProps<"div"> & { action?: ReactNode }) {
  return <div className={cn("flex flex-col items-center gap-4 border-t border-border px-6 py-14 text-center", className)} {...props}>
    <p className="max-w-md text-sm text-muted-foreground">{children}</p>
    {action}
  </div>
}

/** A row of headline-number cards. */
export function MetricGrid({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("grid gap-3 sm:gap-4", className)} {...props} />
}

export function Metric({ label, value, detail, icon, children }: { label: ReactNode; value: ReactNode; detail?: ReactNode; icon?: ReactNode; children?: ReactNode }) {
  return <div className="flex min-w-0 flex-col gap-1.5 rounded-2xl border border-border bg-card p-4 shadow-[0_1px_2px_rgb(0_0_0/0.025)] sm:gap-2 sm:p-5">
    <span className="flex items-center justify-between gap-2 text-sm text-muted-foreground [&_svg]:size-4">{label}{icon}</span>
    <span className="font-heading text-2xl leading-8 font-semibold tracking-[-0.02em] tabular-nums sm:text-[28px] sm:leading-9">{value}</span>
    {children}
    {detail ? <span className="truncate text-[13px] text-muted-foreground" title={typeof detail === "string" ? detail : undefined}>{detail}</span> : null}
  </div>
}

/** Label/value rows split by hairlines, flush with a Section's edges. */
export function FieldList({ className, ...props }: ComponentProps<"dl">) {
  return <dl className={cn("flex flex-col border-t border-border text-sm", className)} {...props} />
}

export function Field({ label, children, mono = false, className }: { label: ReactNode; children: ReactNode; mono?: boolean; className?: string }) {
  return <div className={cn("grid gap-1 border-b border-border px-6 py-3.5 last:border-b-0 sm:grid-cols-[180px_minmax(0,1fr)] sm:gap-4", className)}>
    <dt className="text-muted-foreground">{label}</dt>
    <dd className={cn("min-w-0", mono ? "font-mono text-[13px] leading-5 break-all" : "font-medium")}>{children}</dd>
  </div>
}

/** A soft tinted rounded tile around an icon. */
export function IconTile({ children, tone = "brand", className }: { children: ReactNode; tone?: "brand" | "neutral" | "warning" | "good"; className?: string }) {
  return <span aria-hidden="true" className={cn(
    "flex size-9 shrink-0 items-center justify-center rounded-xl [&_svg]:size-[18px]",
    tone === "brand" && "bg-brand-tint text-brand-foreground",
    tone === "neutral" && "bg-muted text-foreground/70",
    tone === "warning" && "bg-status-warning/12 text-status-warning",
    tone === "good" && "bg-status-good/12 text-status-good",
    className,
  )}>{children}</span>
}
