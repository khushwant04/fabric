"use client"

import * as React from "react"
import {
  BoxesIcon,
  ChartNoAxesCombinedIcon,
  CpuIcon,
  KeyRoundIcon,
  LayoutDashboardIcon,
  NetworkIcon,
  Settings2Icon,
  UserRoundCogIcon,
  UsersIcon,
} from "lucide-react"

import { AccountSwitcher } from "@/components/account-switcher"
import { NavMain, type NavSection } from "@/components/nav-main"
import { NavUser } from "@/components/nav-user"
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
      {
        title: "Identity provider",
        url: "/access/identity-provider",
        icon: <NetworkIcon />,
        scope: "members:read",
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
  const user = context.me.user

  return (
    <Sidebar collapsible="icon" variant="inset" {...props}>
      <SidebarHeader className="p-2">
        <AccountSwitcher context={context} />
      </SidebarHeader>
      <SidebarContent className="gap-2 py-1">
        <NavMain sections={sections} scopes={context.identity.scopes} />
      </SidebarContent>
      <SidebarFooter className="p-2">
        <NavUser
          user={{
            name: user.display_name || user.email || "Fabric operator",
            email: user.email || user.auth0_subject,
          }}
          demo={context.demo}
        />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
