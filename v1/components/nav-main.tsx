"use client"

import type { ReactNode } from "react"
import Link from "next/link"
import { usePathname, useRouter } from "next/navigation"

import { GlidingHighlight, useGlidingHighlight } from "@/components/ui/gliding-highlight"
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
  const { containerRef, highlightRef } = useGlidingHighlight<HTMLElement>({
    itemSelector: "[data-sidebar=menu-button]",
    hoverMode: "nearest",
  })

  return (
    <nav ref={containerRef} className="relative isolate" aria-label="Fabric navigation">
      <GlidingHighlight ref={highlightRef} className="bg-sidebar-accent" />
      {sections.map((section, sectionIndex) => {
        const items = section.items.filter(
          (item) => !item.scope || scopes.includes(item.scope)
        )
        if (!items.length) return null

        return (
          <SidebarGroup
            key={section.label ?? sectionIndex}
            className="z-10 gap-1 px-2 pt-0 pb-4 group-data-[collapsible=icon]/sidebar:px-1.5"
          >
            {section.label ? (
              <SidebarGroupLabel className="h-7 px-2 text-[11px] font-medium text-sidebar-foreground/55 group-data-[collapsible=icon]:opacity-0">
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
                        className="h-9 gap-3 rounded-xl px-2.5 text-[13px] font-normal text-sidebar-foreground/90 transition-colors duration-100 ease-out data-active:bg-sidebar-accent data-active:font-medium [&_svg]:size-[17px] [&_svg]:text-sidebar-foreground/60 [&_svg]:transition-colors [&_svg]:duration-100 hover:[&_svg]:text-sidebar-accent-foreground data-active:[&_svg]:text-sidebar-accent-foreground group-data-[collapsible=icon]/sidebar:size-9!"
                        onClick={() => { if (isMobile) setOpenMobile(false) }}
                        // Warm only the destination the user is about to open.
                        onMouseEnter={() => { if (!isActive) router.prefetch(item.url) }}
                        onFocus={() => { if (!isActive) router.prefetch(item.url) }}
                        aria-current={isActive ? "page" : undefined}
                      >
                        {item.icon}
                        <span className="truncate">{item.title}</span>
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
