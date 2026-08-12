"use client"

import * as React from "react"

import {
  SearchCommandDialog,
  SearchCommandTrigger,
} from "@/components/search-command"
import { UserMenu, type UserMenuUser } from "@/components/user-menu"
import { Button, buttonVariants } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"
import { BellIcon, BookOpenIcon, LifeBuoyIcon, SearchIcon } from "lucide-react"

export function AppHeader({
  user,
  children,
}: {
  user: UserMenuUser
  /** Left-aligned content, typically breadcrumbs. */
  children?: React.ReactNode
}) {
  const [searchOpen, setSearchOpen] = React.useState(false)

  return (
    <header className="sticky top-0 z-40 flex h-12 w-full shrink-0 items-center border-b bg-background px-2 md:px-4">
      <div className="flex min-w-0 items-center gap-2">{children}</div>

      {/* Center search on wide screens */}
      <div className="absolute left-1/2 hidden w-full max-w-xl -translate-x-1/2 px-4 xl:block">
        <SearchCommandTrigger onClick={() => setSearchOpen(true)} />
      </div>

      <div className="ml-auto flex shrink-0 items-center gap-1 sm:gap-1.5">
        {/* Compact search affordance below xl */}
        <Button
          variant="ghost"
          size="icon-sm"
          className="xl:hidden"
          onClick={() => setSearchOpen(true)}
          aria-label="Search"
        >
          <SearchIcon />
        </Button>

        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Notifications"
              />
            }
          >
            <BellIcon />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" side="bottom" className="w-80">
            <DropdownMenuGroup>
              <DropdownMenuLabel>Notifications</DropdownMenuLabel>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <div className="py-4 text-center text-sm text-muted-foreground">
              No new notifications
            </div>
          </DropdownMenuContent>
        </DropdownMenu>

        <a
          href="https://example.com/support"
          target="_blank"
          rel="noopener noreferrer"
          className={cn(
            buttonVariants({ variant: "ghost", size: "icon-sm" }),
            "hidden md:inline-flex"
          )}
        >
          <LifeBuoyIcon />
          <span className="sr-only">Support</span>
        </a>

        <a
          href="https://example.com/docs"
          target="_blank"
          rel="noopener noreferrer"
          className={cn(
            buttonVariants({ variant: "ghost", size: "icon-sm" }),
            "hidden md:inline-flex"
          )}
        >
          <BookOpenIcon />
          <span className="sr-only">Documentation</span>
        </a>

        <UserMenu user={user} />
      </div>

      <SearchCommandDialog open={searchOpen} onOpenChange={setSearchOpen} />
    </header>
  )
}
