"use client"

import * as React from "react"
import { useRouter } from "next/navigation"

import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import { Kbd } from "@/components/ui/kbd"
import { cn } from "@/lib/utils"
import {
  ArrowRightIcon,
  BookOpenIcon,
  BotIcon,
  CreditCardIcon,
  HistoryIcon,
  KeyIcon,
  LayoutDashboardIcon,
  LifeBuoyIcon,
  PlusIcon,
  SearchIcon,
  Settings2Icon,
  StarIcon,
  TerminalSquareIcon,
  UserIcon,
} from "lucide-react"

type SearchCategory = "quick-actions" | "platform" | "account" | "external"

type SearchItem = {
  title: string
  description?: string
  url: string
  icon: React.ReactNode
  category: SearchCategory
  keywords?: string[]
}

// Sample navigation targets. Entries with a "#" url are placeholders until the
// matching route exists, mirroring the placeholder links in the sidebar.
const searchItems: SearchItem[] = [
  {
    title: "New Playground Session",
    description: "Start a fresh prompt session",
    url: "#",
    icon: <PlusIcon />,
    category: "quick-actions",
    keywords: ["new", "create", "prompt", "session"],
  },
  {
    title: "Create API Key",
    description: "Issue a key for programmatic access",
    url: "#",
    icon: <KeyIcon />,
    category: "quick-actions",
    keywords: ["api", "key", "token", "credential"],
  },
  {
    title: "Dashboard",
    description: "Workspace overview",
    url: "/dashboard",
    icon: <LayoutDashboardIcon />,
    category: "platform",
    keywords: ["home", "overview", "dashboard"],
  },
  {
    title: "Playground",
    description: "Experiment with prompts and models",
    url: "#",
    icon: <TerminalSquareIcon />,
    category: "platform",
    keywords: ["prompt", "experiment", "test"],
  },
  {
    title: "History",
    description: "Past playground runs",
    url: "#",
    icon: <HistoryIcon />,
    category: "platform",
    keywords: ["past", "runs", "recent"],
  },
  {
    title: "Starred",
    description: "Saved playground runs",
    url: "#",
    icon: <StarIcon />,
    category: "platform",
    keywords: ["saved", "favorite", "bookmark"],
  },
  {
    title: "Models",
    description: "Browse available models",
    url: "#",
    icon: <BotIcon />,
    category: "platform",
    keywords: ["model", "catalog", "inference"],
  },
  {
    title: "Documentation",
    description: "Guides and API reference",
    url: "#",
    icon: <BookOpenIcon />,
    category: "platform",
    keywords: ["docs", "guide", "reference", "help"],
  },
  {
    title: "Settings",
    description: "Workspace configuration",
    url: "#",
    icon: <Settings2Icon />,
    category: "platform",
    keywords: ["config", "preferences", "workspace"],
  },
  {
    title: "Profile",
    description: "Your account details",
    url: "#",
    icon: <UserIcon />,
    category: "account",
    keywords: ["account", "profile", "me"],
  },
  {
    title: "Billing",
    description: "Plan, usage, and invoices",
    url: "#",
    icon: <CreditCardIcon />,
    category: "account",
    keywords: ["billing", "invoice", "plan", "usage"],
  },
  {
    title: "Support",
    description: "Contact the Fabric team",
    url: "https://example.com/support",
    icon: <LifeBuoyIcon />,
    category: "external",
    keywords: ["support", "contact", "help"],
  },
]

const categoryLabels: Record<SearchCategory, string> = {
  "quick-actions": "Quick Actions",
  platform: "Platform",
  account: "Account",
  external: "External",
}

const categoryOrder: SearchCategory[] = [
  "quick-actions",
  "platform",
  "account",
  "external",
]

/**
 * Header search field. Looks like an input but opens the command palette,
 * so the palette owns all filtering and keyboard navigation.
 */
function SearchCommandTrigger({
  className,
  ...props
}: React.ComponentProps<"button">) {
  return (
    <button
      type="button"
      data-slot="search-command-trigger"
      className={cn(
        "flex h-8 w-full cursor-text items-center gap-2 rounded-md border border-input/60 bg-background px-2.5 text-left text-sm text-muted-foreground transition-colors hover:border-input hover:bg-muted/40 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
        className
      )}
      {...props}
    >
      <SearchIcon className="size-4 shrink-0 opacity-60" />
      <span className="flex-1 truncate">Search pages, actions, resources</span>
      <Kbd className="hidden sm:inline-flex">/</Kbd>
    </button>
  )
}

/**
 * Command palette. Opens on click from a trigger, or with "/" and Cmd/Ctrl+K.
 */
function SearchCommandDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const router = useRouter()

  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const isTyping =
        target instanceof HTMLElement &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)

      if (event.key === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        onOpenChange(!open)
        return
      }

      if (event.key === "/" && !isTyping) {
        event.preventDefault()
        onOpenChange(true)
      }
    }

    document.addEventListener("keydown", onKeyDown)
    return () => document.removeEventListener("keydown", onKeyDown)
  }, [open, onOpenChange])

  const handleSelect = React.useCallback(
    (url: string) => {
      onOpenChange(false)

      if (url.startsWith("http")) {
        window.open(url, "_blank", "noopener,noreferrer")
        return
      }

      // Placeholder targets have no route yet, so just dismiss the palette.
      if (url === "#") {
        return
      }

      router.push(url)
    },
    [onOpenChange, router]
  )

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Search"
      description="Search pages, actions, and resources"
    >
      <Command loop>
        <CommandInput placeholder="Search pages, actions, resources" />
        <CommandList className="max-h-[420px]">
          <CommandEmpty>No results found.</CommandEmpty>
          {categoryOrder.map((category) => {
            const items = searchItems.filter(
              (item) => item.category === category
            )

            if (items.length === 0) {
              return null
            }

            return (
              <CommandGroup key={category} heading={categoryLabels[category]}>
                {items.map((item) => (
                  <CommandItem
                    key={`${item.category}-${item.title}`}
                    keywords={item.keywords}
                    onSelect={() => handleSelect(item.url)}
                  >
                    <div className="flex size-8 shrink-0 items-center justify-center rounded-sm border bg-muted text-muted-foreground group-data-selected/command-item:bg-background">
                      {item.icon}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-medium">{item.title}</div>
                      {item.description && (
                        <div className="truncate text-xs text-muted-foreground">
                          {item.description}
                        </div>
                      )}
                    </div>
                    <ArrowRightIcon className="ml-auto size-3.5 shrink-0 text-muted-foreground opacity-0 group-data-selected/command-item:opacity-100" />
                  </CommandItem>
                ))}
              </CommandGroup>
            )
          })}
        </CommandList>
      </Command>
      <div className="flex items-center gap-4 border-t bg-muted/30 px-3 py-1.5 text-[10px] text-muted-foreground">
        <span className="flex items-center gap-1">
          <Kbd className="h-4 min-w-4 bg-background">↑↓</Kbd> navigate
        </span>
        <span className="flex items-center gap-1">
          <Kbd className="h-4 min-w-4 bg-background">↵</Kbd> select
        </span>
        <span className="flex items-center gap-1">
          <Kbd className="h-4 min-w-4 bg-background">esc</Kbd> close
        </span>
      </div>
    </CommandDialog>
  )
}

/**
 * Self-contained search: input-style trigger plus the palette.
 */
function SearchCommand({ className }: { className?: string }) {
  const [open, setOpen] = React.useState(false)

  return (
    <>
      <SearchCommandTrigger
        className={className}
        onClick={() => setOpen(true)}
      />
      <SearchCommandDialog open={open} onOpenChange={setOpen} />
    </>
  )
}

export { SearchCommand, SearchCommandDialog, SearchCommandTrigger }
