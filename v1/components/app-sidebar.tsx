"use client"

import * as React from "react"

import { NavMain } from "@/components/nav-main"
import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
} from "@/components/ui/sidebar"
import {
  BookOpenIcon,
  BotIcon,
  Settings2Icon,
  TerminalSquareIcon,
} from "lucide-react"

// This is sample data.
const data = {
  navMain: [
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
  ],
}

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  return (
    <Sidebar collapsible="none" {...props} variant="sidebar">
      <SidebarHeader>
        <div className="flex h-12 items-center px-2">
          <span className="text-lg font-semibold tracking-tight">Fabric</span>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <NavMain items={data.navMain} />
      </SidebarContent>
    </Sidebar>
  )
}
