"use client"

import {
  BellIcon,
  BookOpenIcon,
  BoxesIcon,
  CircleHelpIcon,
  HeartPulseIcon,
} from "lucide-react"

import { SearchCommand } from "@/components/search-command"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"
import type { ConsoleContext } from "@/lib/fabric/types"

export function HeaderActions({ context }: { context: ConsoleContext }) {
  return (
    <div className="ml-auto flex items-center gap-1">
      <div className="hidden w-64 xl:block">
        <SearchCommand />
      </div>
      <SearchCommand compact />

      <Badge
        variant="outline"
        className="hidden h-5 rounded-md px-1.5 text-[9px] uppercase sm:flex"
      >
        {context.demo ? "Demo" : "Live"}
      </Badge>

      <a
        href={process.env.NEXT_PUBLIC_FABRIC_DOCS_URL || "/dashboard"}
        className={cn(
          buttonVariants({ variant: "ghost", size: "icon-sm" }),
          "hidden sm:inline-flex"
        )}
        aria-label="Documentation"
      >
        <BookOpenIcon />
      </a>
      <a
        href={process.env.NEXT_PUBLIC_FABRIC_STATUS_URL || "/dashboard"}
        className={cn(
          buttonVariants({ variant: "ghost", size: "icon-sm" }),
          "hidden sm:inline-flex"
        )}
        aria-label="System status"
      >
        <HeartPulseIcon />
      </a>
      <a
        href={process.env.NEXT_PUBLIC_FABRIC_SUPPORT_URL || "/dashboard"}
        className={cn(
          buttonVariants({ variant: "ghost", size: "icon-sm" }),
          "hidden sm:inline-flex"
        )}
        aria-label="Support"
      >
        <CircleHelpIcon />
      </a>

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
        <DropdownMenuContent align="end" className="w-80">
          <DropdownMenuLabel>Notifications</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem className="items-start py-3">
            <BoxesIcon className="mt-0.5" />
            <div>
              <div className="font-medium">Console connected</div>
              <div className="text-xs text-muted-foreground">
                No deployment alerts require attention.
              </div>
            </div>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
