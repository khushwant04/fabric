"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"

import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"

export type NavItem = {
  title: string
  url: string
  icon?: React.ReactNode
}

export function NavMain({ items }: { items: NavItem[] }) {
  const pathname = usePathname()

  return (
    <SidebarMenu>
      {items.map((item) => {
        const isInternal = item.url.startsWith("/")
        const isActive =
          isInternal &&
          (pathname === item.url || pathname.startsWith(`${item.url}/`))

        return (
          <SidebarMenuItem key={item.title}>
            <SidebarMenuButton
              render={
                isInternal ? (
                  <Link href={item.url} />
                ) : (
                  <a href={item.url} />
                )
              }
              isActive={isActive}
              className="h-9 cursor-pointer"
            >
              {item.icon}
              <span>{item.title}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        )
      })}
    </SidebarMenu>
  )
}
