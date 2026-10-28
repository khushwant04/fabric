import Link from "next/link"
import type { ReactNode } from "react"
import { ArrowRightIcon } from "lucide-react"
import { cn } from "@/lib/utils"

// Hexel Studio surfaces: white `card` on the grey page, edged by a 1px `border` ring at
// `radius-surface`, never a shadow. Colour appears only where it carries an outcome.
export function StatCard({ label, value, detail, icon }: { label: string; value: ReactNode; detail: ReactNode; icon: ReactNode }) {
  return <div className="min-w-0 rounded-surface border bg-card p-4">
    <div className="flex items-center justify-between text-muted-foreground"><span className="ui-eyebrow">{label}</span><span className="[&_svg]:size-4 [&_svg]:stroke-[1.75]" aria-hidden="true">{icon}</span></div>
    <div className="ui-display mt-3 tabular-nums">{value}</div>
    <p className="ui-small mt-1 text-muted-foreground">{detail}</p>
  </div>
}

export function SectionCard({ title, description, action, children, className }: { title: string; description?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section aria-label={title} className={cn("min-w-0 rounded-surface border bg-card", className)}>
    <div className="flex flex-wrap items-start justify-between gap-3 px-4 pt-4 sm:px-5 sm:pt-5"><div><h2 className="ui-title">{title}</h2>{description ? <p className="ui-small mt-0.5 text-muted-foreground">{description}</p> : null}</div>{action}</div>
    <div className="p-4 sm:p-5">{children}</div>
  </section>
}

export function SectionLink({ href, children }: { href: string; children: ReactNode }) {
  return <Link href={href} className="ui-small flex items-center gap-1 text-muted-foreground transition-colors duration-(--motion-fast) hover:text-foreground">{children}<ArrowRightIcon className="size-3.5" /></Link>
}

export function ActionCard({ title, description, href, icon }: { title: string; description: string; href: string; icon: ReactNode }) {
  return <Link href={href} className="group flex min-w-0 items-start gap-3 rounded-surface border bg-card p-4 outline-none transition-colors duration-(--motion-fast) hover:bg-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">
    <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border bg-background text-muted-foreground transition-colors duration-(--motion-fast) group-hover:text-foreground [&_svg]:size-4 [&_svg]:stroke-[1.75]" aria-hidden="true">{icon}</span>
    <div className="min-w-0"><h3 className="ui-label">{title}</h3><p className="ui-small mt-0.5 text-muted-foreground">{description}</p></div>
    <ArrowRightIcon className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform duration-(--motion-fast) ease-strong group-hover:translate-x-0.5" />
  </Link>
}
