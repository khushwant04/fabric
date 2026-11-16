"use client"

import Link from "next/link"
import { ChevronsUpDownIcon, LaptopIcon, LogOutIcon, MoonIcon, PlusIcon, Settings2Icon, SunIcon, UsersIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem, useSidebar } from "@/components/ui/sidebar"
import { selectAccount } from "@/app/onboarding/actions"
import { isTheme, useTheme, type Theme } from "@/hooks/use-theme"
import { accountLabel } from "@/lib/fabric/people"
import type { ConsoleContext } from "@/lib/fabric/types"

const themes: Array<{ value: Theme; label: string; icon: typeof SunIcon }> = [
  { value: "light", label: "Light", icon: SunIcon },
  { value: "dark", label: "Dark", icon: MoonIcon },
  { value: "system", label: "System", icon: LaptopIcon },
]

function AccountTile({ name }: { name: string }) {
  return <span aria-hidden="true" className="flex size-6 shrink-0 items-center justify-center rounded-md border border-border bg-muted text-[11px] font-semibold">{name.slice(0, 1).toUpperCase()}</span>
}

// The account card at the foot of the sidebar: who is signed in and to which account.
// Its menu switches account (the same server action as the workspace chooser, which checks
// the membership again), changes the theme and logs out.
export function AccountMenu({ context }: { context: ConsoleContext }) {
  const { isMobile } = useSidebar()
  const { theme, setTheme } = useTheme()
  const user = context.me.user
  const name = user.display_name || user.email || "Fabric operator"
  const membership = context.me.memberships.find((item) => item.account_id === context.account.id)
  const others = context.singleTenant ? [] : context.me.memberships.filter(
    (item) => item.account_id !== context.account.id && item.status === "active"
  )

  return <>
    <form id="account-switcher-form" action={selectAccount} hidden />
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger render={<SidebarMenuButton size="lg" aria-label="Open account menu" className="h-14 gap-3 rounded-xl px-2.5 hover:bg-sidebar-accent/70 data-open:bg-sidebar-accent group-data-[collapsible=icon]/sidebar:size-10! group-data-[collapsible=icon]/sidebar:justify-center" />}>
            <Avatar className="size-8 shrink-0">
              <AvatarFallback className="bg-primary text-[13px] font-medium text-primary-foreground">{name.slice(0, 1).toUpperCase()}</AvatarFallback>
            </Avatar>
            <span className="grid min-w-0 flex-1 text-left leading-tight">
              <span className="truncate text-sm font-semibold">{name}</span>
              <span className="truncate text-[13px] text-muted-foreground">{context.account.name}</span>
            </span>
            <ChevronsUpDownIcon className="ml-auto size-4 shrink-0 text-muted-foreground" />
          </DropdownMenuTrigger>
          <DropdownMenuContent side={isMobile ? "top" : "right"} align="end" sideOffset={10} className="w-72 max-w-[calc(100vw-24px)]">
            <DropdownMenuGroup>
              <DropdownMenuLabel className="px-2 py-2">
                <div className="flex items-center gap-2 text-sm font-medium text-foreground"><span className="truncate">{name}</span>{membership ? <Badge variant="secondary" className="capitalize">{membership.role}</Badge> : null}</div>
                <div className="mt-0.5 truncate text-xs font-normal text-muted-foreground">{user.email || user.auth0_subject}</div>
              </DropdownMenuLabel>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel className="text-xs text-muted-foreground">Account</DropdownMenuLabel>
              <DropdownMenuItem className="gap-2">
                <AccountTile name={context.account.name} />
                <span className="min-w-0 flex-1 truncate">{context.account.name}</span>
                <span className="text-xs text-muted-foreground">Current</span>
              </DropdownMenuItem>
              {others.map((item) => (
                <DropdownMenuItem key={item.account_id} className="w-full gap-2" render={<button type="submit" form="account-switcher-form" name="accountId" value={item.account_id} />}>
                  <AccountTile name={accountLabel(item)} />
                  <span className="min-w-0 flex-1 truncate text-left">{accountLabel(item)}</span>
                  <span className="text-xs text-muted-foreground capitalize">{item.role}</span>
                </DropdownMenuItem>
              ))}
              {context.singleTenant ? null : <>
                <DropdownMenuItem render={<Link href="/onboarding?switch=true" />} className="gap-2"><span className="flex size-6 shrink-0 items-center justify-center rounded-md border border-border"><ChevronsUpDownIcon className="size-3.5" /></span>All accounts</DropdownMenuItem>
                <DropdownMenuItem render={<Link href="/onboarding?switch=true" />} className="gap-2 text-muted-foreground"><span className="flex size-6 shrink-0 items-center justify-center rounded-md border border-border"><PlusIcon className="size-3.5" /></span>Create account</DropdownMenuItem>
              </>}
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel className="text-xs text-muted-foreground">Appearance</DropdownMenuLabel>
              <DropdownMenuRadioGroup aria-label="Appearance" value={theme} onValueChange={(value) => { if (isTheme(value)) setTheme(value) }}>
                {themes.map((item) => <DropdownMenuRadioItem key={item.value} value={item.value} closeOnClick><item.icon />{item.label}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem render={<Link href="/settings" />}><Settings2Icon />Account settings</DropdownMenuItem>
            <DropdownMenuItem render={<Link href="/access/members" />}><UsersIcon />Identity &amp; access</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" render={<a href="/auth/logout" />}><LogOutIcon />Log out</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  </>
}
