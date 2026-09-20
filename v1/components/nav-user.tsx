"use client"

import Link from "next/link"
import {
  CheckIcon,
  ChevronsUpDownIcon,
  LaptopIcon,
  LogOutIcon,
  MoonIcon,
  SettingsIcon,
  SunIcon,
  UserIcon,
} from "lucide-react"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import { useTheme, type Theme } from "@/hooks/use-theme"

const themes: Array<{ value: Theme; label: string; icon: typeof SunIcon }> = [
  { value: "light", label: "Light", icon: SunIcon },
  { value: "dark", label: "Dark", icon: MoonIcon },
  { value: "system", label: "System", icon: LaptopIcon },
]

function initials(name: string) {
  return (
    name
      .split(/\s+/)
      .map((part) => part[0])
      .join("")
      .slice(0, 2)
      .toUpperCase() || "F"
  )
}

export function NavUser({
  user,
  demo,
}: {
  user: { name: string; email: string }
  demo: boolean
}) {
  const { isMobile } = useSidebar()
  const { theme, setTheme } = useTheme()

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <SidebarMenuButton
                size="lg"
                className="gap-2.5 data-open:bg-sidebar-accent"
              />
            }
          >
            <Avatar className="size-7 rounded-md">
              <AvatarFallback className="rounded-md text-[10px]">
                {initials(user.name)}
              </AvatarFallback>
            </Avatar>
            <div className="grid flex-1 text-left leading-tight">
              <span className="truncate text-[13px] font-medium">
                {user.name}
              </span>
              <span className="truncate text-[11px] text-sidebar-foreground/60">
                {user.email}
              </span>
            </div>
            <ChevronsUpDownIcon className="ml-auto size-3.5 opacity-60" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-60"
            side={isMobile ? "bottom" : "right"}
            align="end"
            sideOffset={4}
          >
            <DropdownMenuLabel className="font-normal">
              <div className="truncate text-sm font-medium">{user.name}</div>
              <div className="truncate text-xs text-muted-foreground">
                {user.email}
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem render={<Link href="/settings" />}>
                <UserIcon /> Account profile
              </DropdownMenuItem>
              <DropdownMenuItem render={<Link href="/settings" />}>
                <SettingsIcon /> Console settings
              </DropdownMenuItem>
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>
                  <SunIcon /> Appearance
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent>
                  {themes.map((item) => (
                    <DropdownMenuItem
                      key={item.value}
                      onClick={() => setTheme(item.value)}
                    >
                      <item.icon /> {item.label}
                      {theme === item.value ? (
                        <CheckIcon className="ml-auto" />
                      ) : null}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuSubContent>
              </DropdownMenuSub>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            {demo ? (
              <DropdownMenuItem disabled>
                <LogOutIcon /> Preview session
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                variant="destructive"
                render={<a href="/auth/logout" />}
              >
                <LogOutIcon /> Log out
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
