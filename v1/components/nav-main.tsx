"use client"

import type { ReactNode } from "react"
import Link from "next/link"
import { usePathname, useRouter } from "next/navigation"

import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"

export type NavItem = {
  title: string
  url: string
  icon: ReactNode
  scope?: string
  /** A short pill after the title, e.g. "New". */
  badge?: string
}

export type NavSection = { label?: string; items: NavItem[] }

export function NavMain({
  sections,
  scopes,
}: {
  sections: NavSection[]
  scopes: string[]
}) {
  const pathname = usePathname()
  const router = useRouter()
  const { isMobile, setOpenMobile } = useSidebar()

  return (
    // Plain hover and active states, no gliding highlight. The active page carries an ink
    // bar on its left edge.
    <nav className="flex flex-col gap-4" aria-label="Fabric navigation">
      {sections.map((section, sectionIndex) => {
        const items = section.items.filter(
          (item) => !item.scope || scopes.includes(item.scope)
        )
        if (!items.length) return null

        return (
          <SidebarGroup key={section.label ?? sectionIndex} className={section.label ? "gap-0.5 p-0 group-data-[collapsible=icon]/sidebar:border-t group-data-[collapsible=icon]/sidebar:border-sidebar-border group-data-[collapsible=icon]/sidebar:pt-3" : "gap-0.5 p-0"}>
            {section.label ? (
              <SidebarGroupLabel className="h-7 px-3 font-mono text-[10.5px] font-medium tracking-[0.08em] text-muted-foreground uppercase">
                {section.label}
              </SidebarGroupLabel>
            ) : null}
            <SidebarGroupContent>
              <SidebarMenu highlight={false} className="gap-0.5">
                {items.map((item) => {
                  const isActive =
                    pathname === item.url ||
                    (item.url !== "/dashboard" &&
                      pathname.startsWith(`${item.url}/`))

                  return (
                    <SidebarMenuItem key={item.title}>
                      <SidebarMenuButton
                        render={<Link href={item.url} prefetch={false} />}
                        isActive={isActive}
                        tooltip={item.title}
                        className="h-8 gap-2.5 rounded-sm px-3 text-[14px] font-normal text-sidebar-foreground/80 transition-none hover:bg-sidebar-accent/60 hover:text-sidebar-foreground data-active:bg-sidebar-accent data-active:font-medium data-active:text-sidebar-foreground data-active:shadow-[inset_2px_0_0_var(--sidebar-foreground)] [&_svg]:size-4 [&_svg]:text-muted-foreground data-active:[&_svg]:text-sidebar-foreground group-data-[collapsible=icon]/sidebar:mx-auto group-data-[collapsible=icon]/sidebar:size-10! group-data-[collapsible=icon]/sidebar:justify-center group-data-[collapsible=icon]/sidebar:px-0!"
                        onClick={() => { if (isMobile) setOpenMobile(false) }}
                        // Warm only the destination the user is about to open.
                        onMouseEnter={() => { if (!isActive) router.prefetch(item.url) }}
                        onFocus={() => { if (!isActive) router.prefetch(item.url) }}
                        aria-current={isActive ? "page" : undefined}
                      >
                        {item.icon}
                        <span className="truncate">{item.title}</span>
                        {item.badge ? <span className="ml-auto rounded-xs group-data-[collapsible=icon]/sidebar:hidden bg-brand px-1.5 py-px font-mono text-[10px] font-semibold tracking-wide text-foreground uppercase">{item.badge}</span> : null}
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  )
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        )
      })}
    </nav>
  )
}
