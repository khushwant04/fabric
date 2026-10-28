import Link from "next/link"
import type { ReactNode } from "react"
import { ArrowRightIcon } from "lucide-react"

// A quiet link for a Panel's header action: muted text that turns ink on hover, instantly.
export function PanelLink({ href, children }: { href: string; children: ReactNode }) {
  return <Link href={href} className="flex items-center gap-1 rounded-md text-[13px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">{children}<ArrowRightIcon className="size-3.5" /></Link>
}

// One destination in a Panel: an icon, a name and a line about it, the whole row a link.
export function PanelLinkRow({ href, title, description, icon }: { href: string; title: string; description: string; icon: ReactNode }) {
  return <Link href={href} className="group relative flex min-h-12 items-center gap-3 px-4 py-3 text-sm outline-none hover:bg-muted/45 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset first:rounded-t-[var(--radius-surface)] last:rounded-b-[var(--radius-surface)] [&+&]:before:absolute [&+&]:before:inset-x-4 [&+&]:before:top-0 [&+&]:before:h-px [&+&]:before:bg-border">
    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground group-hover:text-foreground [&_svg]:size-4" aria-hidden="true">{icon}</span>
    <span className="min-w-0"><span className="block font-medium">{title}</span><span className="block truncate text-[13px] text-muted-foreground">{description}</span></span>
    <ArrowRightIcon className="ml-auto size-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
  </Link>
}
