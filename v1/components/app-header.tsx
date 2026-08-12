"use client"

import * as React from "react"
import Link from "next/link"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Activity01Icon,
  Book02Icon,
  CustomerSupportIcon,
  Menu01Icon,
  Notification01Icon,
  Search01Icon,
} from "@hugeicons/core-free-icons"

import { SearchCommand } from "@/components/search-command"
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
import { useSidebar } from "@/components/ui/sidebar"
import { cn } from "@/lib/utils"

export function AppHeader({
  user,
  children,
}: {
  user: UserMenuUser
  children?: React.ReactNode
}) {
  const [showMobileSearch, setShowMobileSearch] = React.useState(false)
  const { toggleSidebar, state } = useSidebar()
  const sidebarOpen = state === "expanded"

  return (
    <header className="sticky top-0 z-50 h-12 w-full border-b border-border bg-background">
      <div className="relative flex h-full items-center px-2 md:px-4">
        <div className="flex shrink-0 items-center gap-2 md:-ml-1">
          <Button
            variant="ghost"
            size="icon"
            className={
              sidebarOpen
                ? "size-8 shrink-0 bg-foreground/4 text-foreground hover:bg-foreground/8"
                : "size-8 shrink-0 bg-foreground/4 text-foreground/50 hover:bg-foreground/8"
            }
            onClick={toggleSidebar}
            aria-label="Toggle Sidebar"
            aria-expanded={sidebarOpen}
          >
            <HugeiconsIcon icon={Menu01Icon} className="size-[13px]" />
          </Button>

          <Link
            href="/dashboard"
            className="flex items-center font-semibold tracking-tight text-foreground transition-colors hover:text-foreground/80"
          >
            <span className="text-sm font-semibold">Fabric</span>
          </Link>

          {children ? (
            <div className="hidden min-w-0 items-center gap-2 md:flex">
              {children}
            </div>
          ) : null}
        </div>

        <div className="absolute left-1/2 hidden w-full max-w-xl -translate-x-1/2 xl:block">
          <SearchCommand />
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-1.5 sm:gap-2">
          <Button
            variant="ghost"
            size="icon"
            className="size-8 xl:hidden"
            onClick={() => setShowMobileSearch(true)}
            aria-label="Search"
          >
            <HugeiconsIcon icon={Search01Icon} className="size-4" />
          </Button>

          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8"
                  aria-label="Notifications"
                />
              }
            >
              <HugeiconsIcon icon={Notification01Icon} className="size-4" />
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
            href="https://example.com/status"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="System status"
            className={cn(
              buttonVariants({ variant: "ghost", size: "icon" }),
              "hidden size-8 md:inline-flex"
            )}
          >
            <HugeiconsIcon
              icon={Activity01Icon}
              className="size-4 text-primary"
            />
            <span className="sr-only">System status</span>
          </a>

          <a
            href="https://example.com/support"
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              buttonVariants({ variant: "ghost", size: "icon" }),
              "hidden size-8 md:inline-flex"
            )}
          >
            <HugeiconsIcon icon={CustomerSupportIcon} className="size-4" />
            <span className="sr-only">Support</span>
          </a>

          <a
            href="https://example.com/docs"
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              buttonVariants({ variant: "ghost", size: "icon" }),
              "hidden size-8 md:inline-flex"
            )}
          >
            <HugeiconsIcon icon={Book02Icon} className="size-4" />
            <span className="sr-only">Docs</span>
          </a>

          <UserMenu user={user} />
        </div>
      </div>

      {showMobileSearch ? (
        <div className="fixed inset-0 z-50 bg-background xl:hidden">
          <div className="flex items-center border-b p-3">
            <div className="flex-1">
              <SearchCommand
                defaultOpen
                onOpenChange={(open) => {
                  if (!open) setShowMobileSearch(false)
                }}
              />
            </div>
          </div>
        </div>
      ) : null}
    </header>
  )
}
