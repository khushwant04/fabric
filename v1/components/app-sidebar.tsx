"use client"

import * as React from "react"
import {
  BoxesIcon,
  ChartNoAxesCombinedIcon,
  CpuIcon,
  KeyRoundIcon,
  LayoutDashboardIcon,
  NetworkIcon,
  FlaskConicalIcon,
  Settings2Icon,
  UserRoundCogIcon,
  UsersIcon,
} from "lucide-react"

import { NavMain, type NavSection } from "@/components/nav-main"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarRail,
} from "@/components/ui/sidebar"
import type { ConsoleContext } from "@/lib/fabric/types"

const sections: NavSection[] = [
  {
    items: [
      { title: "Overview", url: "/dashboard", icon: <LayoutDashboardIcon /> },
      { title: "Architecture", url: "/architecture", icon: <NetworkIcon /> },
    ],
  },
  {
    label: "Infrastructure",
    items: [
      {
        title: "Deployments",
        url: "/deployments",
        icon: <BoxesIcon />,
        scope: "deployments:read",
      },
      {
        title: "Inference stamps",
        url: "/stamps",
        icon: <CpuIcon />,
        scope: "stamps:read",
      },
      {
        title: "Usage",
        url: "/usage",
        icon: <ChartNoAxesCombinedIcon />,
        scope: "deployments:read",
      },
      {
        title: "Playground",
        url: "/playground",
        icon: <FlaskConicalIcon />,
        scope: "deployments:read",
      },
    ],
  },
  {
    label: "Identity & access",
    items: [
      {
        title: "Members",
        url: "/access/members",
        icon: <UsersIcon />,
        scope: "members:read",
      },
      {
        title: "Service principals",
        url: "/access/service-principals",
        icon: <UserRoundCogIcon />,
        scope: "api-keys:read",
      },
      {
        title: "API keys",
        url: "/access/api-keys",
        icon: <KeyRoundIcon />,
        scope: "api-keys:read",
      },
    ],
  },
  {
    label: "Administration",
    items: [
      {
        title: "Account settings",
        url: "/settings",
        icon: <Settings2Icon />,
        scope: "accounts:read",
      },
    ],
  },
]

export function AppSidebar({
  context,
  ...props
}: React.ComponentProps<typeof Sidebar> & { context: ConsoleContext }) {
  return (
    <Sidebar id="fabric-sidebar" collapsible="icon" variant="inset" className="fabric-sidebar" {...props}>
      <SidebarHeader className="h-2 p-0" />
      <SidebarContent className="gap-1 px-1 pt-1">
        <NavMain sections={sections} scopes={context.identity.scopes} />
      </SidebarContent>
      <SidebarFooter className="px-5 py-5 text-[11px] text-sidebar-foreground/45 group-data-[collapsible=icon]:hidden">Fabric Operator Console</SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
