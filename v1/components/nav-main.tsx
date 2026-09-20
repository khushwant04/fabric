"use client"

import type { ReactNode } from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"

import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
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

  return (
    <>
      {sections.map((section, sectionIndex) => {
        const items = section.items.filter(
          (item) => !item.scope || scopes.includes(item.scope)
        )
        if (!items.length) return null

        return (
          <SidebarGroup
            key={section.label ?? sectionIndex}
            className="gap-1 px-2 py-1"
          >
            {section.label ? (
              <SidebarGroupLabel className="h-7 px-2 text-[11px] font-medium text-sidebar-foreground/55 group-data-[collapsible=icon]:opacity-0">
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
                        render={<Link href={item.url} />}
                        isActive={isActive}
                        tooltip={item.title}
                        className="h-8 gap-2.5 rounded-md px-2 text-[13px] font-normal data-[active=true]:font-medium"
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
    </>
  )
}
