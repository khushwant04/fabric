"use client"

import * as React from "react"
import { useRouter } from "next/navigation"

import { useTheme, type Theme } from "@/hooks/use-theme"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  CheckIcon,
  ChevronsUpDownIcon,
  CopyIcon,
  LogOutIcon,
  MonitorIcon,
  MoonIcon,
  SunIcon,
} from "lucide-react"

type UserMenuUser = {
  name: string
  email: string
  avatar?: string
}

const themeLabels: Record<Theme, string> = {
  light: "Light",
  dark: "Dark",
  system: "System",
}

function getInitials(user: UserMenuUser) {
  const fromName = user.name
    .split(" ")
    .filter(Boolean)
    .map((part) => part[0])
    .join("")
    .toUpperCase()
    .slice(0, 2)

  return fromName || user.email[0]?.toUpperCase() || "?"
}

export function UserMenu({ user }: { user: UserMenuUser }) {
  const router = useRouter()
  const { theme, resolvedTheme, setTheme } = useTheme()
  const [copied, setCopied] = React.useState(false)

  const initials = getInitials(user)
  const ThemeIcon =
    theme === "system" ? MonitorIcon : resolvedTheme === "dark" ? MoonIcon : SunIcon

  const copyEmail = async () => {
    try {
      await navigator.clipboard.writeText(user.email)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard access can be blocked; leave the label unchanged.
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            className="h-8 gap-1.5 px-1 aria-expanded:bg-muted sm:px-1.5"
            aria-label="Open account menu"
          />
        }
      >
        <Avatar size="sm">
          <AvatarImage src={user.avatar} alt="" />
          <AvatarFallback className="text-[10px]">{initials}</AvatarFallback>
        </Avatar>
        <span className="hidden max-w-[120px] truncate text-sm font-medium text-foreground sm:block">
          {user.name}
        </span>
        <ChevronsUpDownIcon className="hidden size-3.5 shrink-0 text-muted-foreground sm:block" />
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" side="bottom" className="w-60">
        {/* DropdownMenuLabel renders Base UI's Menu.GroupLabel, which requires
            an enclosing group. */}
        <DropdownMenuGroup>
          <DropdownMenuLabel className="flex items-center gap-2 p-0 py-1.5 font-normal">
            <Avatar>
              <AvatarImage src={user.avatar} alt="" />
              <AvatarFallback className="text-[10px]">{initials}</AvatarFallback>
            </Avatar>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-foreground">
                {user.name}
              </span>
              <span className="block truncate text-xs text-muted-foreground">
                {user.email}
              </span>
            </span>
          </DropdownMenuLabel>
        </DropdownMenuGroup>

        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem closeOnClick={false} onClick={copyEmail}>
            {copied ? <CheckIcon /> : <CopyIcon />}
            {copied ? "Copied" : "Copy email"}
          </DropdownMenuItem>

          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <ThemeIcon />
              <span className="flex-1">Appearance</span>
              <span className="text-xs text-muted-foreground">
                {themeLabels[theme]}
              </span>
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent className="w-40">
              <DropdownMenuRadioGroup
                value={theme}
                onValueChange={(value) => setTheme(value as Theme)}
              >
                <DropdownMenuRadioItem value="light">
                  <SunIcon />
                  Light
                </DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="dark">
                  <MoonIcon />
                  Dark
                </DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="system">
                  <MonitorIcon />
                  System
                </DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </DropdownMenuGroup>

        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive" onClick={() => router.push("/")}>
          <LogOutIcon />
          Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export type { UserMenuUser }
