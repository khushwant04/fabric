"use client"

import * as React from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { ChevronRightIcon } from "lucide-react"

const labels: Record<string, string> = {
  dashboard: "Home",
  architecture: "Architecture",
  playground: "Playground",
  deployments: "Deployments",
  new: "Create",
  stamps: "Inference stamps",
  usage: "Usage",
  access: "Identity & access",
  members: "Members",
  "service-principals": "Service principals",
  "api-keys": "API keys",
  settings: "Account settings",
  agents: "Agents",
  runs: "Runs",
}

function labelFor(segment: string, previous?: string) {
  if (previous === "runs") return "Run trace"
  if (labels[segment]) return labels[segment]
  if (/^[0-9a-f-]{12,}$/i.test(segment) || segment.startsWith("dep_")) {
    return "Details"
  }
  return segment.replaceAll("-", " ").replace(/^./, (c) => c.toUpperCase())
}

// The path to a nested page (Deployments › Details). Top-level pages show nothing: the
// sidebar already says where you are. Section-only segments (access, agents) are plain text.
export function ConsoleBreadcrumbs() {
  const pathname = usePathname()
  const segments = pathname.split("/").filter(Boolean)
  const linkable = segments.map((segment, index) => !["access", "agents"].includes(segment) && index < segments.length - 1)
  if (segments.length < 2 || segments.every((segment, index) => index === segments.length - 1 || !linkable[index])) return null

  return (
    <nav aria-label="Breadcrumb">
      <ol className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
        {segments.map((segment, index) => {
          const href = `/${segments.slice(0, index + 1).join("/")}`
          const isLast = index === segments.length - 1
          const label = labelFor(segment, segments[index - 1])
          return (
            <React.Fragment key={href}>
              <li>
                {isLast ? <span aria-current="page" className="text-foreground">{label}</span>
                  : linkable[index] ? <Link href={href} className="rounded-sm outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">{label}</Link>
                    : <span>{label}</span>}
              </li>
              {isLast ? null : <li aria-hidden="true"><ChevronRightIcon className="size-3.5" /></li>}
            </React.Fragment>
          )
        })}
      </ol>
    </nav>
  )
}
