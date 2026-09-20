"use client"

import Link from "next/link"
import { ChevronsUpDownIcon, PlusIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import type { ConsoleContext } from "@/lib/fabric/types"

export function AccountSwitcher({ context }: { context: ConsoleContext }) {
  const { isMobile } = useSidebar()
  const membership = context.me.memberships.find(
    (item) => item.account_id === context.account.id
  )

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <SidebarMenuButton
                size="lg"
                className="gap-2.5 data-open:bg-sidebar-accent data-open:text-sidebar-accent-foreground"
              />
            }
          >
            <div className="flex aspect-square size-7 shrink-0 items-center justify-center rounded-md bg-sidebar-primary text-[11px] font-semibold text-sidebar-primary-foreground">
              {context.account.name.slice(0, 1).toUpperCase()}
            </div>
            <div className="grid flex-1 text-left leading-tight">
              <span className="truncate text-[13px] font-medium">
                {context.account.name}
              </span>
              <span className="truncate text-[11px] text-sidebar-foreground/60">
                {context.account.slug}
              </span>
            </div>
            <ChevronsUpDownIcon className="ml-auto size-3.5 opacity-60" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-64"
            align="start"
            side={isMobile ? "bottom" : "right"}
            sideOffset={4}
          >
            <DropdownMenuGroup>
              <DropdownMenuLabel className="text-xs text-muted-foreground">
                Current account
              </DropdownMenuLabel>
              <DropdownMenuItem className="gap-2">
                <div className="flex size-6 shrink-0 items-center justify-center rounded-md border text-[10px] font-semibold">
                  {context.account.name.slice(0, 1).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm">{context.account.name}</div>
                  <div className="truncate text-xs text-muted-foreground">
                    {context.identity.principal_type.replaceAll("_", " ")}
                  </div>
                </div>
                {membership ? (
                  <Badge variant="outline" className="capitalize">
                    {membership.role}
                  </Badge>
                ) : null}
              </DropdownMenuItem>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem
                render={<Link href="/onboarding?switch=true" />}
                className="gap-2"
              >
                <div className="flex size-6 shrink-0 items-center justify-center rounded-md border">
                  <ChevronsUpDownIcon className="size-3.5" />
                </div>
                Switch account
              </DropdownMenuItem>
              <DropdownMenuItem
                render={<Link href="/onboarding?switch=true" />}
                className="gap-2"
              >
                <div className="flex size-6 shrink-0 items-center justify-center rounded-md border">
                  <PlusIcon className="size-3.5" />
                </div>
                <span className="text-muted-foreground">Create account</span>
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
