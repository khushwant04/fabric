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
    <nav className="flex flex-col gap-3" aria-label="Fabric navigation">
      {sections.map((section, sectionIndex) => {
        const items = section.items.filter(
          (item) => !item.scope || scopes.includes(item.scope)
        )
        if (!items.length) return null

        return (
          <SidebarGroup key={section.label ?? sectionIndex} className="gap-0.5 p-0">
            {section.label ? (
              <SidebarGroupLabel className="h-8 px-3 text-[11px] font-medium tracking-[0.08em] text-muted-foreground uppercase">
                {section.label}
              </SidebarGroupLabel>
            ) : null}
            <SidebarGroupContent>
              <SidebarMenu className="gap-0.5">
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
                        className="h-9 gap-3 rounded-lg px-3 text-[15px] font-normal text-sidebar-foreground/85 transition-colors duration-100 ease-out hover:bg-sidebar-accent/70 hover:text-sidebar-foreground data-active:bg-sidebar-accent data-active:font-medium data-active:text-sidebar-foreground [&_svg]:size-4 [&_svg]:text-muted-foreground data-active:[&_svg]:text-sidebar-foreground group-data-[collapsible=icon]/sidebar:size-10! group-data-[collapsible=icon]/sidebar:justify-center"
                        onClick={() => { if (isMobile) setOpenMobile(false) }}
                        // Warm only the destination the user is about to open.
                        onMouseEnter={() => { if (!isActive) router.prefetch(item.url) }}
                        onFocus={() => { if (!isActive) router.prefetch(item.url) }}
                        aria-current={isActive ? "page" : undefined}
                      >
                        {item.icon}
                        <span className="truncate">{item.title}</span>
                        {item.badge ? <span className="ml-auto rounded-full bg-brand-tint px-1.5 py-px text-[10px] font-semibold tracking-wide text-brand-foreground uppercase">{item.badge}</span> : null}
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
