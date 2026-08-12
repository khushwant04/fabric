"use client"

import * as React from "react"
import { useRouter } from "next/navigation"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  ArrowDown01Icon,
  ClockIcon,
  ComputerIcon,
  CreditCardIcon,
  LanguageCircleIcon,
  LogoutIcon,
  MoonIcon,
  Settings01Icon,
  SunIcon,
  UserIcon,
} from "@hugeicons/core-free-icons"

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

type UserMenuUser = {
  name: string
  email: string
  avatar?: string
}

const languageOptions = [
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
]

const timezoneOptions = [
  { value: "UTC", label: "UTC" },
  { value: "America/New_York", label: "New York" },
  { value: "Europe/London", label: "London" },
  { value: "Asia/Kolkata", label: "Kolkata" },
]

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

function getOptionLabel(
  options: { value: string; label: string }[],
  value: string
) {
  return options.find((option) => option.value === value)?.label ?? value
}

export function UserMenu({ user }: { user: UserMenuUser }) {
  const router = useRouter()
  const { theme, resolvedTheme, setTheme } = useTheme()
  const [locale, setLocale] = React.useState("en")
  const [timezone, setTimezone] = React.useState("UTC")

  const initials = getInitials(user)
  const themeIcon =
    theme === "system"
      ? ComputerIcon
      : resolvedTheme === "dark"
        ? MoonIcon
        : SunIcon

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            className="h-8 gap-1.5 px-1 aria-expanded:bg-muted/70 sm:px-1.5"
            aria-label="Open account menu"
          />
        }
      >
        <Avatar className="size-6 shrink-0">
          <AvatarImage src={user.avatar} alt="" />
          <AvatarFallback className="text-[10px]">{initials}</AvatarFallback>
        </Avatar>
        <span className="hidden max-w-[120px] truncate text-sm font-medium text-foreground sm:block">
          {user.name}
        </span>
        <HugeiconsIcon
          icon={ArrowDown01Icon}
          className="hidden size-3.5 shrink-0 text-muted-foreground sm:block"
        />
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" side="bottom" className="w-60">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="flex items-center gap-2 py-1.5">
            <Avatar className="size-7 shrink-0">
              <AvatarImage src={user.avatar} alt="" />
              <AvatarFallback className="text-[10px]">
                {initials}
              </AvatarFallback>
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
        <DropdownMenuItem>
          <HugeiconsIcon icon={UserIcon} />
          Profile
        </DropdownMenuItem>
        <DropdownMenuItem>
          <HugeiconsIcon icon={CreditCardIcon} />
          Billing
        </DropdownMenuItem>

        <DropdownMenuSeparator />
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <HugeiconsIcon icon={themeIcon} />
            <span className="flex-1">Appearance</span>
            <span className="max-w-[72px] truncate text-xs text-muted-foreground">
              {themeLabels[theme]}
            </span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="w-40">
            <DropdownMenuRadioGroup
              value={theme}
              onValueChange={(value) => setTheme(value as Theme)}
            >
              <DropdownMenuRadioItem value="light">
                <HugeiconsIcon icon={SunIcon} />
                Light
              </DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="dark">
                <HugeiconsIcon icon={MoonIcon} />
                Dark
              </DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="system">
                <HugeiconsIcon icon={ComputerIcon} />
                System
              </DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>

        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <HugeiconsIcon icon={LanguageCircleIcon} />
            <span className="flex-1">Language</span>
            <span className="max-w-[72px] truncate text-xs text-muted-foreground">
              {getOptionLabel(languageOptions, locale)}
            </span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="w-44">
            <DropdownMenuRadioGroup value={locale} onValueChange={setLocale}>
              {languageOptions.map((option) => (
                <DropdownMenuRadioItem
                  key={option.value}
                  value={option.value}
                >
                  {option.label}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>

        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <HugeiconsIcon icon={ClockIcon} />
            <span className="flex-1">Timezone</span>
            <span className="max-w-[72px] truncate text-xs text-muted-foreground">
              {getOptionLabel(timezoneOptions, timezone)}
            </span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="w-48">
            <DropdownMenuRadioGroup
              value={timezone}
              onValueChange={setTimezone}
            >
              {timezoneOptions.map((option) => (
                <DropdownMenuRadioItem
                  key={option.value}
                  value={option.value}
                >
                  {option.label}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>

        <DropdownMenuItem>
          <HugeiconsIcon icon={Settings01Icon} />
          All preferences
        </DropdownMenuItem>

        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onClick={() => router.push("/")}
        >
          <HugeiconsIcon icon={LogoutIcon} />
          Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export type { UserMenuUser }
