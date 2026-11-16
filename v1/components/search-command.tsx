"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import {
  BotIcon,
  BoxesIcon,
  ChartNoAxesCombinedIcon,
  CpuIcon,
  KeyRoundIcon,
  HouseIcon,
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
  { label: "Home", href: "/dashboard", icon: HouseIcon, group: "Platform" },
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

export function SearchCommand() {
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
      <Button
        variant="outline"
        aria-label="Search pages and actions"
        className="h-9 w-full justify-start gap-2 px-3 text-sm font-normal text-muted-foreground shadow-none group-data-[collapsible=icon]/sidebar:size-9 group-data-[collapsible=icon]/sidebar:justify-center group-data-[collapsible=icon]/sidebar:px-0"
        onClick={() => setOpen(true)}
      >
        <SearchIcon />
        <span className="group-data-[collapsible=icon]/sidebar:hidden">Search</span>
        <span className="ml-auto flex items-center gap-1 group-data-[collapsible=icon]/sidebar:hidden">
          <Kbd>Ctrl</Kbd><Kbd>K</Kbd>
        </span>
      </Button>
      <CommandDialog open={open} onOpenChange={setOpen} title="Search Fabric" description="Go to a console page or start an action" className="top-[16%] w-[calc(100%-2rem)] max-w-2xl! overflow-hidden rounded-2xl border border-border p-0 shadow-[0_24px_64px_rgb(0_0_0/0.14)]">
        <Command className="bg-popover">
          <div className="flex items-center gap-2 border-b border-border pr-3 [&_[data-slot=command-input-wrapper]]:flex-1 [&_[data-slot=command-input-wrapper]]:border-0 [&_[data-slot=input-group]]:h-14 [&_[data-slot=input-group]]:border-0 [&_[data-slot=input-group]]:bg-transparent [&_[data-slot=input-group]]:shadow-none [&_input]:text-[15px]">
            <CommandInput placeholder="Search pages and actions…" />
            <Kbd>Esc</Kbd>
          </div>
          <CommandList className="max-h-[min(28rem,60vh)] p-2">
            <CommandEmpty>No page or action matches.</CommandEmpty>
            {groups.map((group) => (
              <CommandGroup
                heading={group}
                key={group}
                className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-3 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:tracking-[0.08em] [&_[cmdk-group-heading]]:text-muted-foreground [&_[cmdk-group-heading]]:uppercase"
              >
                {entries.filter((entry) => entry.group === group).map((entry) => (
                  <CommandItem key={`${group}-${entry.label}`} value={`${entry.label} ${entry.hint ?? ""} ${group}`} onSelect={() => select(entry.href)} className="h-11 gap-3 rounded-lg px-2">
                    <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-card text-muted-foreground [&_svg]:size-4">
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
          <div className="flex items-center gap-4 border-t border-border bg-muted/40 px-4 py-2.5 text-xs text-muted-foreground">
            <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd>Navigate</span>
            <span className="flex items-center gap-1"><Kbd>↵</Kbd>Open</span>
            <span className="ml-auto flex items-center gap-1"><Kbd>Ctrl</Kbd><Kbd>K</Kbd>Toggle</span>
          </div>
        </Command>
      </CommandDialog>
    </>
  )
}
