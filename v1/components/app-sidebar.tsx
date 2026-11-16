"use client"

import * as React from "react"
import Link from "next/link"
import {
  BotIcon,
  BoxesIcon,
  ChartNoAxesCombinedIcon,
  CpuIcon,
  KeyRoundIcon,
  HouseIcon,
  NetworkIcon,
  FlaskConicalIcon,
  FingerprintIcon,
  ListTreeIcon,
  ScrollTextIcon,
  Settings2Icon,
  UserRoundCogIcon,
  UsersIcon,
} from "lucide-react"

import { AccountMenu } from "@/components/account-switcher"
import { FabricMark } from "@/components/fabric-mark"
import { NavMain, type NavSection } from "@/components/nav-main"
import { SearchCommand } from "@/components/search-command"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import type { ConsoleContext } from "@/lib/fabric/types"

const sections: NavSection[] = [
  {
    items: [
      { title: "Home", url: "/dashboard", icon: <HouseIcon /> },
      { title: "Architecture", url: "/architecture", icon: <NetworkIcon /> },
    ],
  },
  {
    label: "Infrastructure",
    items: [
      { title: "Deployments", url: "/deployments", icon: <BoxesIcon />, scope: "deployments:read" },
      { title: "Inference stamps", url: "/stamps", icon: <CpuIcon />, scope: "stamps:read" },
      { title: "Usage", url: "/usage", icon: <ChartNoAxesCombinedIcon />, scope: "deployments:read" },
      { title: "Playground", url: "/playground", icon: <FlaskConicalIcon />, scope: "deployments:read" },
    ],
  },
  {
    label: "Agents",
    items: [
      { title: "Runs", url: "/agents/runs", icon: <ListTreeIcon />, scope: "deployments:read" },
      { title: "Agent playground", url: "/agents/playground", icon: <BotIcon />, scope: "deployments:read", badge: "New" },
    ],
  },
  {
    label: "Identity & access",
    items: [
      { title: "Members", url: "/access/members", icon: <UsersIcon />, scope: "members:read" },
      { title: "Service principals", url: "/access/service-principals", icon: <UserRoundCogIcon />, scope: "api-keys:read" },
      { title: "API keys", url: "/access/api-keys", icon: <KeyRoundIcon />, scope: "api-keys:read" },
      { title: "Identity provider", url: "/access/identity-provider", icon: <FingerprintIcon />, scope: "accounts:read" },
    ],
  },
  {
    label: "Administration",
    items: [
      { title: "Account settings", url: "/settings", icon: <Settings2Icon />, scope: "accounts:read" },
      { title: "Audit log", url: "/audit", icon: <ScrollTextIcon />, scope: "audit:read" },
    ],
  },
]

export function AppSidebar({
  context,
  ...props
}: React.ComponentProps<typeof Sidebar> & { context: ConsoleContext }) {
  return (
    <Sidebar id="fabric-sidebar" collapsible="icon" className="border-sidebar-border" {...props}>
      <SidebarHeader className="gap-3 px-4 pt-4 pb-2 group-data-[collapsible=icon]/sidebar:items-center group-data-[collapsible=icon]/sidebar:px-2">
        {/* Collapsed, the rail keeps the mark on top and stacks the toggle under it. */}
        <div className="flex h-9 items-center justify-between gap-2 group-data-[collapsible=icon]/sidebar:h-auto group-data-[collapsible=icon]/sidebar:flex-col group-data-[collapsible=icon]/sidebar:gap-3">
          <Link href="/dashboard" aria-label="Fabric home" className="flex min-w-0 items-center gap-2.5 rounded-lg text-[18px] font-semibold tracking-[-0.03em] outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <FabricMark /><span className="group-data-[collapsible=icon]/sidebar:hidden">Fabric</span>
          </Link>
          <SidebarTrigger className="text-muted-foreground hover:text-foreground" />
        </div>
        <SearchCommand />
      </SidebarHeader>
      <SidebarContent className="gap-0 px-2 pt-2 pb-4 group-data-[collapsible=icon]/sidebar:px-2">
        <NavMain sections={sections} scopes={context.identity.scopes} />
      </SidebarContent>
      <SidebarFooter className="border-t border-sidebar-border p-3 group-data-[collapsible=icon]/sidebar:items-center group-data-[collapsible=icon]/sidebar:px-2 group-data-[collapsible=icon]/sidebar:py-3">
        <AccountMenu context={context} />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
