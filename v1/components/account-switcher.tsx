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
import { selectAccount } from "@/app/onboarding/actions"
import { accountLabel } from "@/lib/fabric/people"
import type { ConsoleContext } from "@/lib/fabric/types"

export function AccountSwitcher({ context }: { context: ConsoleContext }) {
  const membership = context.me.memberships.find(
    (item) => item.account_id === context.account.id
  )
  // Named from /v1/me, so switching needs no extra request per account. Each item submits
  // the same server action as the workspace chooser, which checks the membership again.
  const others = context.me.memberships.filter(
    (item) => item.account_id !== context.account.id && item.status === "active"
  )

  return (
      <>
        <form id="account-switcher-form" action={selectAccount} hidden />
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
            {others.length ? <>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                <DropdownMenuLabel className="text-xs text-muted-foreground">
                  Switch to
                </DropdownMenuLabel>
                {others.map((item) => (
                  <DropdownMenuItem
                    key={item.account_id}
                    className="gap-2"
                    render={<button type="submit" form="account-switcher-form" name="accountId" value={item.account_id} />}
                  >
                    <div className="flex size-6 shrink-0 items-center justify-center rounded-md border text-[10px] font-semibold">
                      {accountLabel(item).slice(0, 1).toUpperCase()}
                    </div>
                    <span className="min-w-0 flex-1 truncate text-left text-sm">{accountLabel(item)}</span>
                    <Badge variant="outline" className="capitalize">{item.role}</Badge>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuGroup>
            </> : null}
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem
                render={<Link href="/onboarding?switch=true" />}
                className="gap-2"
              >
                <div className="flex size-6 shrink-0 items-center justify-center rounded-md border">
                  <ChevronsUpDownIcon className="size-3.5" />
                </div>
                All accounts
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
      </>
  )
}
