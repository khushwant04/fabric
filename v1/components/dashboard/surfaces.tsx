import Link from "next/link"
import type { ReactNode } from "react"
import { ArrowUpRightIcon } from "lucide-react"
import { cn } from "@/lib/utils"

// Console-ui uses tinted statistics/action surfaces and outlined content sections.
export function StatCard({ label, value, detail, icon }: { label: string; value: ReactNode; detail: ReactNode; icon: ReactNode }) {
  return <div className="min-w-0 rounded-2xl bg-muted/60 p-5 dark:bg-muted/40">
    <div className="flex items-center justify-between text-xs text-muted-foreground"><span>{label}</span><span className="[&_svg]:size-4" aria-hidden="true">{icon}</span></div>
    <div className="mt-4 text-[28px] font-semibold leading-8 tracking-tight tabular-nums">{value}</div>
    <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{detail}</p>
  </div>
}

export function SectionCard({ title, description, action, children, className }: { title: string; description?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section aria-label={title} className={cn("min-w-0 rounded-2xl border bg-card", className)}>
    <div className="flex flex-wrap items-start justify-between gap-3 px-5 pt-5 sm:px-6"><div><h2 className="text-[15px] font-medium tracking-tight">{title}</h2>{description ? <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p> : null}</div>{action}</div>
    <div className="p-5 sm:p-6">{children}</div>
  </section>
}

export function ActionCard({ title, description, href, icon }: { title: string; description: string; href: string; icon: ReactNode }) {
  return <Link href={href} className="group flex min-w-0 items-start gap-3 rounded-2xl bg-muted/60 p-5 outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring dark:bg-muted/40 dark:hover:bg-muted/70">
    <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-background text-muted-foreground [&_svg]:size-4" aria-hidden="true">{icon}</span><div className="min-w-0"><h3 className="text-[13px] font-medium">{title}</h3><p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p></div><ArrowUpRightIcon className="ml-auto size-4 shrink-0 text-muted-foreground opacity-50 transition-opacity group-hover:opacity-100" />
  </Link>
}
