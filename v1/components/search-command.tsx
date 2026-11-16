"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import {
  BotIcon,
  BoxesIcon,
  ChartNoAxesCombinedIcon,
  CpuIcon,
  KeyRoundIcon,
  LayoutDashboardIcon,
  NetworkIcon,
  FlaskConicalIcon,
  FingerprintIcon,
  ListTreeIcon,
  PlusIcon,
  SearchIcon,
  ScrollTextIcon,
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

type Entry = { label: string; href: string; icon: typeof BoxesIcon; group: string; hint?: string }

const actions: Entry[] = [
  { label: "Create deployment", href: "/deployments/new", icon: PlusIcon, group: "Actions", hint: "Deploy a model on a stamp" },
  { label: "Enroll a stamp", href: "/stamps", icon: CpuIcon, group: "Actions", hint: "Connect a GPU cluster" },
  { label: "Create API key", href: "/access/api-keys", icon: KeyRoundIcon, group: "Actions", hint: "Scoped key for an app" },
  { label: "Open playground", href: "/playground", icon: FlaskConicalIcon, group: "Actions", hint: "Try a deployed model" },
]

const pages: Entry[] = [
  { label: "Overview", href: "/dashboard", icon: LayoutDashboardIcon, group: "Platform" },
  { label: "Architecture", href: "/architecture", icon: NetworkIcon, group: "Platform" },
  { label: "Deployments", href: "/deployments", icon: BoxesIcon, group: "Infrastructure" },
  { label: "Inference stamps", href: "/stamps", icon: CpuIcon, group: "Infrastructure" },
  { label: "Usage", href: "/usage", icon: ChartNoAxesCombinedIcon, group: "Infrastructure" },
  { label: "Playground", href: "/playground", icon: FlaskConicalIcon, group: "Infrastructure" },
  { label: "Runs", href: "/agents/runs", icon: ListTreeIcon, group: "Agents" },
  { label: "Agent playground", href: "/agents/playground", icon: BotIcon, group: "Agents" },
  { label: "Members", href: "/access/members", icon: UsersIcon, group: "Identity & access" },
  { label: "Service principals", href: "/access/service-principals", icon: UserRoundCogIcon, group: "Identity & access" },
  { label: "API keys", href: "/access/api-keys", icon: KeyRoundIcon, group: "Identity & access" },
  { label: "Identity provider", href: "/access/identity-provider", icon: FingerprintIcon, group: "Identity & access" },
  { label: "Account settings", href: "/settings", icon: Settings2Icon, group: "Administration" },
  { label: "Audit log", href: "/audit", icon: ScrollTextIcon, group: "Administration" },
]

const groups = ["Actions", "Platform", "Infrastructure", "Agents", "Identity & access", "Administration"]
const entries = [...actions, ...pages]

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
        <Button variant="ghost" size="icon-sm" className="xl:hidden" aria-label="Search" onClick={() => setOpen(true)}>
          <SearchIcon />
        </Button>
      ) : (
        <Button
          variant="outline"
          className="h-9 w-full justify-start border-sidebar-border bg-background px-3 text-[13px] font-normal text-muted-foreground shadow-none"
          onClick={() => setOpen(true)}
        >
          <SearchIcon className="size-3.5" />
          <span>Search...</span>
          <span className="ml-auto flex items-center gap-1">
            <Kbd>Ctrl</Kbd><Kbd>K</Kbd>
          </span>
        </Button>
      )}
      <CommandDialog open={open} onOpenChange={setOpen} title="Search Fabric" description="Go to a console page or start an action" className="top-[18%] w-[calc(100%-2rem)] max-w-2xl! border border-border shadow-2xl">
        <Command className="bg-popover">
          <div className="flex items-center gap-2 border-b border-border pr-3 [&_[data-slot=command-input-wrapper]]:flex-1 [&_[data-slot=command-input-wrapper]]:border-0 [&_[data-slot=input-group]]:h-12 [&_[data-slot=input-group]]:bg-transparent [&_input]:text-sm">
            <CommandInput placeholder="Search pages and actions…" />
            <Kbd>Esc</Kbd>
          </div>
          <CommandList className="max-h-[min(26rem,60vh)] p-1">
            <CommandEmpty>No page or action matches.</CommandEmpty>
            {groups.map((group) => (
              <CommandGroup
                heading={group}
                key={group}
                className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-3 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:uppercase"
              >
                {entries.filter((entry) => entry.group === group).map((entry) => (
                  <CommandItem key={`${group}-${entry.label}`} value={`${entry.label} ${entry.hint ?? ""} ${group}`} onSelect={() => select(entry.href)} className="h-10 gap-3 px-2">
                    <span className="flex size-6 shrink-0 items-center justify-center border border-border text-muted-foreground [&_svg]:size-3.5">
                      <entry.icon />
                    </span>
                    <span className="truncate text-sm">{entry.label}</span>
                    {entry.hint ? <span className="hidden truncate text-xs text-muted-foreground sm:inline">{entry.hint}</span> : null}
                    <CommandShortcut className="font-mono text-[11px]">{entry.href}</CommandShortcut>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
          <div className="flex items-center gap-4 border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
            <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd>Navigate</span>
            <span className="flex items-center gap-1"><Kbd>↵</Kbd>Open</span>
            <span className="ml-auto flex items-center gap-1"><Kbd>Ctrl</Kbd><Kbd>K</Kbd>Toggle</span>
          </div>
        </Command>
      </CommandDialog>
    </>
  )
}
