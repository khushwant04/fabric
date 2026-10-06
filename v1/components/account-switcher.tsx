"use client"

import Link from "next/link"
import { Building2Icon, ChevronDownIcon, ChevronsUpDownIcon, PlusIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
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
import type { ConsoleContext } from "@/lib/fabric/types"

export function AccountSwitcher({ context }: { context: ConsoleContext }) {
  const membership = context.me.memberships.find(
    (item) => item.account_id === context.account.id
  )

  return (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button variant="ghost" className="h-9 w-full min-w-0 justify-start gap-2 rounded-lg px-2 text-[13px] font-normal" aria-label="Switch account" />
            }
          >
            <Building2Icon className="hidden size-4 shrink-0 text-muted-foreground sm:block" strokeWidth={1.75} /><span className="truncate">{context.account.name}</span><ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-72 rounded-xl p-1.5"
            align="start"
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
  )
}
