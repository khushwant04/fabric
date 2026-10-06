"use client"

import Link from "next/link"
import { ChevronDownIcon, LayersIcon, LogOutIcon, MonitorIcon, MoonIcon, Settings2Icon, SunIcon, UserIcon } from "lucide-react"

import { AccountSwitcher } from "@/components/account-switcher"
import { SearchCommand } from "@/components/search-command"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuGroup,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { isTheme, useTheme } from "@/hooks/use-theme"
import type { ConsoleContext } from "@/lib/fabric/types"

export function ThemeMenu() {
  const { theme, resolvedTheme, setTheme } = useTheme()
  const Icon = theme === "system" ? MonitorIcon : resolvedTheme === "dark" ? MoonIcon : SunIcon
  return <DropdownMenu>
    <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" className="size-8 rounded-lg" aria-label="Change theme" />}><Icon className="size-4" strokeWidth={1.75} /></DropdownMenuTrigger>
    <DropdownMenuContent align="end" className="w-40 rounded-xl p-1.5">
      <DropdownMenuGroup><DropdownMenuLabel>Appearance</DropdownMenuLabel></DropdownMenuGroup>
      <DropdownMenuRadioGroup aria-label="Appearance" value={theme} onValueChange={(value) => { if (isTheme(value)) setTheme(value) }}>
        <DropdownMenuRadioItem value="light" closeOnClick className="h-9 rounded-lg"><SunIcon />Light</DropdownMenuRadioItem>
        <DropdownMenuRadioItem value="dark" closeOnClick className="h-9 rounded-lg"><MoonIcon />Dark</DropdownMenuRadioItem>
        <DropdownMenuRadioItem value="system" closeOnClick className="h-9 rounded-lg"><MonitorIcon />System</DropdownMenuRadioItem>
      </DropdownMenuRadioGroup>
    </DropdownMenuContent>
  </DropdownMenu>
}

export function AppHeader({ context }: { context: ConsoleContext }) {
  const user = context.me.user
  const name = user.display_name || user.email || "Fabric operator"
  const membership = context.me.memberships.find((item) => item.account_id === context.account.id)
  return <header className="fabric-topbar" aria-label="App header">
    <Link href="/dashboard" aria-label="Fabric home" className="fabric-brand flex items-center gap-2.5 rounded-lg text-[17px] font-semibold tracking-[-0.045em] outline-none transition-opacity hover:opacity-75 focus-visible:ring-2 focus-visible:ring-ring">
      <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground"><LayersIcon className="size-4" strokeWidth={1.8} /></span>Fabric
    </Link>
    <div className="min-w-0 max-w-64 flex-1 lg:flex-none">{context.singleTenant ? <span className="block truncate px-2 text-[13px]" aria-label="Workspace">{context.account.name}</span> : <AccountSwitcher context={context} />}</div>
    <div className="ml-auto flex min-w-0 items-center gap-1.5">
      <div className="mr-3 hidden w-64 xl:block"><SearchCommand /></div>
      <SearchCommand compact /><ThemeMenu />
      <DropdownMenu>
        <DropdownMenuTrigger render={<Button variant="ghost" className="h-9 gap-2 rounded-full px-1.5" aria-label="Open user menu" />}>
          <Avatar className="size-7"><AvatarFallback className="bg-primary text-[11px] text-primary-foreground">{name.slice(0, 1).toUpperCase()}</AvatarFallback></Avatar>
          <span className="hidden max-w-28 truncate text-[13px] xl:block">{name}</span><ChevronDownIcon className="hidden size-3.5 text-muted-foreground sm:block" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" sideOffset={8} className="w-72 max-w-[calc(100vw-24px)] rounded-xl p-1.5">
          <DropdownMenuGroup><DropdownMenuLabel className="p-3"><div className="flex items-center gap-2 text-[13px] text-foreground">{name}{membership ? <Badge variant="secondary" className="h-5 capitalize">{membership.role}</Badge> : null}</div><div className="mt-1 truncate text-xs font-normal text-muted-foreground">{user.email || user.auth0_subject}</div></DropdownMenuLabel></DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem className="h-9 rounded-lg" render={<Link href="/settings" />}><UserIcon />Account profile</DropdownMenuItem>
          <DropdownMenuItem className="h-9 rounded-lg" render={<Link href="/access/members" />}><Settings2Icon />Identity &amp; access</DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem className="h-9 rounded-lg" variant="destructive" render={<a href="/auth/logout" />}><LogOutIcon />Log out</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </header>
}
