"use client"

import * as React from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"

const labels: Record<string, string> = {
  dashboard: "Overview",
  deployments: "Deployments",
  new: "Create",
  stamps: "Inference stamps",
  usage: "Usage",
  access: "Identity & access",
  members: "Members",
  "service-principals": "Service principals",
  "api-keys": "API keys",
  "identity-provider": "Identity provider",
  settings: "Account settings",
}

function labelFor(segment: string) {
  if (labels[segment]) return labels[segment]
  if (/^[0-9a-f-]{12,}$/i.test(segment) || segment.startsWith("dep_")) {
    return "Details"
  }
  return segment.replaceAll("-", " ").replace(/^./, (c) => c.toUpperCase())
}

export function ConsoleBreadcrumbs() {
  const pathname = usePathname()
  const segments = pathname.split("/").filter(Boolean)

  return (
    <Breadcrumb>
      <BreadcrumbList className="gap-1.5 text-[13px] sm:gap-1.5">
        {segments.map((segment, index) => {
          const href = `/${segments.slice(0, index + 1).join("/")}`
          const isLast = index === segments.length - 1

          return (
            <React.Fragment key={href}>
              <BreadcrumbItem>
                {isLast ? (
                  <BreadcrumbPage className="font-medium">
                    {labelFor(segment)}
                  </BreadcrumbPage>
                ) : (
                  <BreadcrumbLink
                    render={<Link href={href} />}
                    className="text-muted-foreground"
                  >
                    {labelFor(segment)}
                  </BreadcrumbLink>
                )}
              </BreadcrumbItem>
              {isLast ? null : <BreadcrumbSeparator />}
            </React.Fragment>
          )
        })}
      </BreadcrumbList>
    </Breadcrumb>
  )
}
