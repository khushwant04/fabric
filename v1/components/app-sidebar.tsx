"use client"

import * as React from "react"

import { NavMain, type NavItem } from "@/components/nav-main"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
} from "@/components/ui/sidebar"
import {
  BookOpenIcon,
  BotIcon,
  LayoutDashboardIcon,
  Settings2Icon,
  TerminalSquareIcon,
} from "lucide-react"

const navigation: NavItem[] = [
  {
    title: "Dashboard",
    url: "/dashboard",
    icon: <LayoutDashboardIcon />,
  },
  {
    title: "Playground",
    url: "#",
    icon: <TerminalSquareIcon />,
  },
  {
    title: "Models",
    url: "#",
    icon: <BotIcon />,
  },
  {
    title: "Documentation",
    url: "#",
    icon: <BookOpenIcon />,
  },
  {
    title: "Settings",
    url: "#",
    icon: <Settings2Icon />,
  },
]

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  return (
    <Sidebar collapsible="offcanvas" variant="sidebar" {...props}>
      <SidebarContent className="px-1 py-3">
        <SidebarGroup>
          <NavMain items={navigation} />
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="p-3" />
    </Sidebar>
  )
}
