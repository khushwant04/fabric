"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import {
  BoxesIcon,
  ChartNoAxesCombinedIcon,
  CpuIcon,
  KeyRoundIcon,
  LayoutDashboardIcon,
  NetworkIcon,
  SearchIcon,
  Settings2Icon,
  UserRoundCogIcon,
  UsersIcon,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command"
import { Kbd } from "@/components/ui/kbd"

const items = [
  { label: "Overview", href: "/dashboard", icon: LayoutDashboardIcon, group: "Platform" },
  { label: "Deployments", href: "/deployments", icon: BoxesIcon, group: "Infrastructure" },
  { label: "Inference stamps", href: "/stamps", icon: CpuIcon, group: "Infrastructure" },
  { label: "Usage", href: "/usage", icon: ChartNoAxesCombinedIcon, group: "Infrastructure" },
  { label: "Members", href: "/access/members", icon: UsersIcon, group: "Identity & access" },
  { label: "Service principals", href: "/access/service-principals", icon: UserRoundCogIcon, group: "Identity & access" },
  { label: "API keys", href: "/access/api-keys", icon: KeyRoundIcon, group: "Identity & access" },
  { label: "Identity provider", href: "/access/identity-provider", icon: NetworkIcon, group: "Identity & access" },
  { label: "Account settings", href: "/settings", icon: Settings2Icon, group: "Administration" },
]

export function SearchCommand({ compact = false }: { compact?: boolean }) {
  const [open, setOpen] = useState(false)
  const router = useRouter()

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        setOpen((value) => !value)
      }
    }
    window.addEventListener("keydown", listener)
    return () => window.removeEventListener("keydown", listener)
  }, [])

  const select = (href: string) => {
    setOpen(false)
    router.push(href)
  }

  return (
    <>
      {compact ? (
        <Button variant="ghost" size="icon-sm" className="lg:hidden" aria-label="Search" onClick={() => setOpen(true)}>
          <SearchIcon />
        </Button>
      ) : (
        <Button
          variant="outline"
          className="h-8 w-full justify-start bg-background px-2.5 text-xs font-normal text-muted-foreground shadow-xs"
          onClick={() => setOpen(true)}
        >
          <SearchIcon className="size-3.5" />
          <span>Search...</span>
          <span className="ml-auto flex items-center gap-1">
            <Kbd>Ctrl</Kbd><Kbd>K</Kbd>
          </span>
        </Button>
      )}
      <CommandDialog open={open} onOpenChange={setOpen} title="Search Fabric" description="Navigate to a console page">
        <Command>
          <CommandInput placeholder="Search pages and resources..." />
          <CommandList>
            <CommandEmpty>No results found.</CommandEmpty>
            {["Platform", "Infrastructure", "Identity & access", "Administration"].map((group) => (
              <CommandGroup heading={group} key={group}>
                {items.filter((item) => item.group === group).map((item) => (
                  <CommandItem key={item.href} value={`${item.label} ${group}`} onSelect={() => select(item.href)}>
                    <item.icon />
                    <span>{item.label}</span>
                    <CommandShortcut>↵</CommandShortcut>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </CommandDialog>
    </>
  )
}
