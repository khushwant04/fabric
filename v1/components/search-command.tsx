"use client"

import * as React from "react"
import { useRouter } from "next/navigation"
import { HugeiconsIcon, type IconSvgElement } from "@hugeicons/react"
import {
  Activity01Icon,
  ArrowRight01Icon,
  BookOpen01Icon,
  BotIcon,
  ClockIcon,
  CreditCardIcon,
  CustomerSupportIcon,
  Home01Icon,
  Key01Icon,
  Search01Icon,
  Settings01Icon,
  Terminal,
  UserIcon,
} from "@hugeicons/core-free-icons"

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"

interface NavItem {
  title: string
  description?: string
  url: string
  icon: IconSvgElement
  category: string
  keywords?: string[]
  defaultShortcut?: boolean
}

const navItems: NavItem[] = [
  {
    title: "New Playground Session",
    description: "Start a fresh prompt session",
    url: "#",
    icon: Terminal,
    category: "quick-actions",
    keywords: ["new", "create", "prompt", "session"],
    defaultShortcut: true,
  },
  {
    title: "Create API Key",
    description: "Issue a key for programmatic access",
    url: "#",
    icon: Key01Icon,
    category: "quick-actions",
    keywords: ["api", "key", "token", "credential"],
    defaultShortcut: true,
  },
  {
    title: "Dashboard",
    description: "Workspace overview",
    url: "/dashboard",
    icon: Home01Icon,
    category: "platform",
    keywords: ["home", "overview", "dashboard"],
    defaultShortcut: true,
  },
  {
    title: "Playground",
    description: "Experiment with prompts and models",
    url: "#",
    icon: Terminal,
    category: "platform",
    keywords: ["prompt", "experiment", "test"],
    defaultShortcut: true,
  },
  {
    title: "History",
    description: "Past playground runs",
    url: "#",
    icon: ClockIcon,
    category: "recent",
    keywords: ["past", "runs", "recent"],
    defaultShortcut: true,
  },
  {
    title: "Models",
    description: "Browse available models",
    url: "#",
    icon: BotIcon,
    category: "platform",
    keywords: ["model", "catalog", "inference"],
    defaultShortcut: true,
  },
  {
    title: "Recent Activity",
    description: "Latest workspace changes",
    url: "/dashboard",
    icon: Activity01Icon,
    category: "recent",
    keywords: ["activity", "recent", "events"],
    defaultShortcut: true,
  },
  {
    title: "Documentation",
    description: "Guides and API reference",
    url: "#",
    icon: BookOpen01Icon,
    category: "platform",
    keywords: ["docs", "guide", "reference", "help"],
  },
  {
    title: "Settings",
    description: "Workspace configuration",
    url: "#",
    icon: Settings01Icon,
    category: "settings",
    keywords: ["config", "preferences", "workspace"],
  },
  {
    title: "Profile",
    description: "Your account details",
    url: "#",
    icon: UserIcon,
    category: "settings",
    keywords: ["account", "profile", "me"],
  },
  {
    title: "Billing",
    description: "Plan, usage, and invoices",
    url: "#",
    icon: CreditCardIcon,
    category: "settings",
    keywords: ["billing", "invoice", "plan", "usage"],
  },
  {
    title: "Support",
    description: "Contact the Fabric team",
    url: "https://example.com/support",
    icon: CustomerSupportIcon,
    category: "external",
    keywords: ["support", "contact", "help"],
  },
]

const categoryLabels: Record<string, string> = {
  "quick-actions": "Quick Actions",
  recent: "Recent Resources",
  platform: "Platform",
  settings: "Settings",
  external: "External",
}

interface SearchCommandProps {
  defaultOpen?: boolean
  onOpenChange?: (open: boolean) => void
}

export function SearchCommand({
  defaultOpen = false,
  onOpenChange,
}: SearchCommandProps = {}) {
  const [open, setOpen] = React.useState(defaultOpen)
  const [search, setSearch] = React.useState("")
  const [selectedIndex, setSelectedIndex] = React.useState(0)
  const [showSkeleton, setShowSkeleton] = React.useState(defaultOpen)
  const [isFocused, setIsFocused] = React.useState(false)
  const router = useRouter()
  const triggerInputRef = React.useRef<HTMLInputElement>(null)

  const handleOpenChange = React.useCallback(
    (newOpen: boolean) => {
      setOpen(newOpen)
      onOpenChange?.(newOpen)

      if (newOpen) {
        setSelectedIndex(0)
        setShowSkeleton(true)
        window.setTimeout(() => triggerInputRef.current?.focus(), 0)
      } else {
        setSearch("")
      }
    },
    [onOpenChange]
  )

  const handleSelect = React.useCallback(
    (url: string) => {
      handleOpenChange(false)
      setSearch("")

      if (url.startsWith("http")) {
        window.open(url, "_blank", "noopener,noreferrer")
      } else if (url !== "#") {
        router.push(url)
      }
    },
    [handleOpenChange, router]
  )

  const filteredItems = React.useMemo(() => {
    if (!search) return navItems.filter((item) => item.defaultShortcut)

    const query = search.toLowerCase()
    return navItems.filter(
      (item) =>
        item.title.toLowerCase().includes(query) ||
        item.description?.toLowerCase().includes(query) ||
        item.category.includes(query) ||
        item.keywords?.some((keyword) =>
          keyword.toLowerCase().includes(query)
        )
    )
  }, [search])

  const grouped = React.useMemo(() => {
    const groups: Record<string, NavItem[]> = {}
    for (const item of filteredItems) {
      if (!groups[item.category]) groups[item.category] = []
      groups[item.category].push(item)
    }
    return groups
  }, [filteredItems])

  React.useEffect(() => {
    const down = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const isTyping =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.isContentEditable

      if (event.key === "/" && !isTyping) {
        event.preventDefault()
        handleOpenChange(true)
      }

      if (event.key === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        handleOpenChange(!open)
      }
    }

    document.addEventListener("keydown", down)
    return () => document.removeEventListener("keydown", down)
  }, [handleOpenChange, open])

  React.useEffect(() => {
    if (!open) return

    const handleKeyDown = (event: KeyboardEvent) => {
      if (document.activeElement !== triggerInputRef.current) return
      if (filteredItems.length === 0) return

      if (event.key === "ArrowDown") {
        event.preventDefault()
        setSelectedIndex((index) =>
          Math.min(index + 1, filteredItems.length - 1)
        )
      } else if (event.key === "ArrowUp") {
        event.preventDefault()
        setSelectedIndex((index) => Math.max(index - 1, 0))
      } else if (event.key === "Enter") {
        event.preventDefault()
        const item = filteredItems[selectedIndex]
        if (item) handleSelect(item.url)
      }
    }

    document.addEventListener("keydown", handleKeyDown)
    return () => document.removeEventListener("keydown", handleKeyDown)
  }, [filteredItems, handleSelect, open, selectedIndex])

  React.useEffect(() => {
    if (!showSkeleton) return
    const timer = window.setTimeout(() => setShowSkeleton(false), 400)
    return () => window.clearTimeout(timer)
  }, [showSkeleton])

  return (
    <div className="w-full">
      <Popover open={open} onOpenChange={handleOpenChange} modal={false}>
        <PopoverTrigger
          render={
            <div
              data-search-trigger
              className={cn(
                "relative flex h-9 w-full cursor-text items-center rounded-md border bg-background px-3 text-left transition-[border-color,box-shadow] duration-150",
                isFocused && "border-foreground/20 shadow-overlay"
              )}
              onClick={() => {
                if (!open) handleOpenChange(true)
              }}
            />
          }
        >
          <HugeiconsIcon
            icon={Search01Icon}
            className="mr-2 size-4 shrink-0 text-muted-foreground"
          />
          <input
            ref={triggerInputRef}
            type="text"
            placeholder="Search pages, actions, resources"
            value={search}
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
            onChange={(event) => {
              setSearch(event.target.value)
              setSelectedIndex(0)
              if (!open) handleOpenChange(true)
            }}
            onFocus={() => {
              setIsFocused(true)
              if (!open) handleOpenChange(true)
            }}
            onBlur={() => setIsFocused(false)}
            className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          <span className="ml-2 hidden shrink-0 items-center gap-1.5 text-xs text-muted-foreground sm:flex">
            <kbd className="rounded-sm border bg-muted px-1.5 py-0.5 text-[10px]">
              /
            </kbd>
          </span>
        </PopoverTrigger>

        <PopoverContent
          className="w-[var(--anchor-width)] min-w-[320px] border-0 p-0 shadow-overlay ring-1 ring-border/60"
          align="start"
          sideOffset={4}
        >
          <div
            className={cn(
              "overflow-y-auto scroll-smooth transition-all",
              search ? "max-h-[640px]" : "max-h-[300px]"
            )}
          >
            {showSkeleton ? (
              <div className="space-y-1 p-2">
                {Array.from({ length: 6 }).map((_, index) => (
                  <div
                    key={index}
                    className="flex items-center gap-3 rounded-sm px-2 py-2"
                  >
                    <div className="size-8 shrink-0 animate-pulse rounded-md bg-muted" />
                    <div className="flex-1 space-y-1.5">
                      <div className="h-3.5 w-2/3 animate-pulse rounded-sm bg-muted" />
                      <div className="h-2.5 w-1/2 animate-pulse rounded-sm bg-muted" />
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="p-1.5">
                {Object.entries(grouped).map(([category, items]) => (
                  <div key={category}>
                    <div className="u-label px-2 py-1.5 text-muted-foreground">
                      {categoryLabels[category] || category}
                    </div>
                    {items.map((item) => {
                      const index = filteredItems.indexOf(item)
                      const isSelected = selectedIndex === index

                      return (
                        <button
                          key={`${item.category}-${item.title}`}
                          type="button"
                          className={cn(
                            "flex w-full items-center gap-3 rounded-sm px-2 py-2 text-left text-sm transition-colors",
                            isSelected &&
                              "bg-sidebar-accent text-sidebar-accent-foreground"
                          )}
                          onClick={() => handleSelect(item.url)}
                          onMouseEnter={() => setSelectedIndex(index)}
                        >
                          <div
                            className={cn(
                              "flex size-8 shrink-0 items-center justify-center rounded-sm border",
                              isSelected ? "bg-background" : "bg-muted"
                            )}
                          >
                            <HugeiconsIcon
                              icon={item.icon}
                              className={cn(
                                "size-4",
                                isSelected
                                  ? "text-foreground"
                                  : "text-muted-foreground"
                              )}
                            />
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="truncate font-medium">
                              {item.title}
                            </div>
                            {item.description ? (
                              <div className="truncate text-xs text-muted-foreground">
                                {item.description}
                              </div>
                            ) : null}
                          </div>
                          {isSelected ? (
                            <HugeiconsIcon
                              icon={ArrowRight01Icon}
                              className="size-3.5 shrink-0 text-muted-foreground"
                            />
                          ) : null}
                        </button>
                      )
                    })}
                  </div>
                ))}

                {search && filteredItems.length === 0 ? (
                  <div className="py-8 text-center text-sm text-muted-foreground">
                    No results for &quot;{search}&quot;
                  </div>
                ) : null}
              </div>
            )}
          </div>

          <div className="flex items-center gap-4 border-t bg-muted/30 px-3 py-1.5 text-[10px] text-muted-foreground">
            <span className="flex items-center gap-1">
              <kbd className="rounded-sm border bg-background px-1 py-0.5">
                ↑↓
              </kbd>{" "}
              navigate
            </span>
            <span className="flex items-center gap-1">
              <kbd className="rounded-sm border bg-background px-1 py-0.5">
                ↵
              </kbd>{" "}
              select
            </span>
            <span className="flex items-center gap-1">
              <kbd className="rounded-sm border bg-background px-1 py-0.5">
                esc
              </kbd>{" "}
              close
            </span>
          </div>
        </PopoverContent>
      </Popover>
    </div>
  )
}
