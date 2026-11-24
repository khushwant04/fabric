import Link from "next/link"
import type { ComponentProps, ReactNode } from "react"
import { ArrowRightIcon } from "lucide-react"

import { cn } from "@/lib/utils"

// The console's building blocks, shared by every page: paper panels drawn with a visible
// hairline and small corners, a 15px heading with a muted line under it, monospace labels,
// headline numbers as bracketed tiles, and label/value rows split by hairlines. No shadows.

/** The small uppercase label above a group of cards, e.g. "QUICK START". */
export const labelClass = "font-mono text-[11px] font-medium tracking-[0.06em] text-muted-foreground uppercase"

/** A card: heading, optional description and action, then its content. */
export function Section({ title, description, action, className, children, ...props }: Omit<ComponentProps<"section">, "title"> & { title: ReactNode; description?: ReactNode; action?: ReactNode }) {
  return <section className={cn("flex min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-card [&>[data-slot=empty]]:rounded-none [&>[data-slot=empty]]:border-0 [&>[data-slot=empty]]:border-t [&>[data-slot=empty]]:border-solid [&>[data-slot=table-frame]]:border-t", className)} {...props}>
    <header className="flex shrink-0 flex-wrap items-start justify-between gap-x-4 gap-y-2 px-5 pt-4 pb-3.5">
      <div className="min-w-0">
        <h2 className="flex min-w-0 items-center gap-2 text-[15px] leading-6 font-medium tracking-[-0.005em]">{title}</h2>
        {description ? <p className="mt-0.5 max-w-3xl text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
    </header>
    {children}
  </section>
}

/** Padded content inside a Section, under its heading. */
export function SectionBody({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex min-w-0 flex-col gap-4 px-5 pb-5 text-sm", className)} {...props} />
}

/** A strip under a Section's content: pagination, a note, secondary actions. */
export function SectionFooter({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex min-h-12 items-center justify-between gap-2 border-t border-border px-5 py-2.5 text-sm text-muted-foreground", className)} {...props} />
}

/** An underlined text link with an arrow, e.g. "All deployments →". */
export function SectionLink({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  return <Link href={href} className={cn("group/link inline-flex items-center gap-1 font-mono text-[12px] font-medium text-foreground underline decoration-foreground/25 underline-offset-4 outline-none hover:decoration-foreground focus-visible:ring-2 focus-visible:ring-ring rounded-sm", className)}>
    {children}<ArrowRightIcon className="size-3.5" />
  </Link>
}

/** A centred muted sentence for an empty or unavailable Section, with an optional action. */
export function SectionEmpty({ className, action, children, ...props }: ComponentProps<"div"> & { action?: ReactNode }) {
  return <div className={cn("flex flex-col items-center gap-4 border-t border-border px-5 py-12 text-center", className)} {...props}>
    <p className="max-w-md text-sm text-muted-foreground">{children}</p>
    {action}
  </div>
}

/** A row of headline-number cards. */
export function MetricGrid({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("grid gap-3", className)} {...props} />
}

export function Metric({ label, value, detail, icon, children }: { label: ReactNode; value: ReactNode; detail?: ReactNode; icon?: ReactNode; children?: ReactNode }) {
  return <div className="fabric-brackets flex min-w-0 flex-col gap-1.5 rounded-sm border border-border bg-card p-4 sm:gap-2 sm:px-5">
    <span className="flex items-center justify-between gap-2 font-mono text-[11px] font-medium tracking-[0.06em] text-muted-foreground uppercase [&_svg]:size-3.5">{label}{icon}</span>
    <span className="font-heading text-2xl leading-8 font-medium tracking-[-0.02em] tabular-nums sm:text-[28px] sm:leading-9">{value}</span>
    {children}
    {detail ? <span className="truncate font-mono text-[12px] text-muted-foreground" title={typeof detail === "string" ? detail : undefined}>{detail}</span> : null}
  </div>
}

/** Label/value rows split by hairlines, flush with a Section's edges. */
export function FieldList({ className, ...props }: ComponentProps<"dl">) {
  return <dl className={cn("flex flex-col border-t border-border text-sm", className)} {...props} />
}

export function Field({ label, children, mono = false, className }: { label: ReactNode; children: ReactNode; mono?: boolean; className?: string }) {
  return <div className={cn("grid gap-1 border-b border-border px-5 py-3 last:border-b-0 sm:grid-cols-[180px_minmax(0,1fr)] sm:gap-4", className)}>
    <dt className="text-muted-foreground">{label}</dt>
    <dd className={cn("min-w-0", mono ? "font-mono text-[13px] leading-5 break-all" : "font-medium")}>{children}</dd>
  </div>
}

/** A soft tinted rounded tile around an icon. */
export function IconTile({ children, tone = "brand", className }: { children: ReactNode; tone?: "brand" | "neutral" | "warning" | "good"; className?: string }) {
  return <span aria-hidden="true" className={cn(
    "flex size-9 shrink-0 items-center justify-center rounded-md border [&_svg]:size-[18px]",
    tone === "brand" && "border-border bg-card text-foreground",
    tone === "neutral" && "border-border bg-muted text-foreground/70",
    tone === "warning" && "border-status-warning/25 bg-status-warning/10 text-status-warning",
    tone === "good" && "border-status-good/25 bg-status-good/10 text-status-good",
    className,
  )}>{children}</span>
}
