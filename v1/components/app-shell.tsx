"use client"

import type { ReactNode } from "react"

import { AppSidebar } from "@/components/app-sidebar"
import { ConsoleBreadcrumbs } from "@/components/console/breadcrumbs"
import { AppHeader } from "@/components/app-header"
import { Separator } from "@/components/ui/separator"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import type { ConsoleContext } from "@/lib/fabric/types"

export function AppShell({
  children,
  context,
}: {
  children: ReactNode
  context: ConsoleContext
}) {
  return (
    <div className="fabric-layout"><AppHeader context={context} /><SidebarProvider
      className="fabric-sidebar-wrapper"
      style={
        {
          "--sidebar-width": "16rem",
          "--sidebar-width-icon": "3.25rem",
        } as React.CSSProperties
      }
    >
      <AppSidebar context={context} />
      <SidebarInset className="fabric-inset md:peer-data-[variant=inset]:mt-0 md:peer-data-[variant=inset]:rounded-2xl" aria-label="Console content">
        <header className="fabric-content-header" aria-label="Content header">
          <SidebarTrigger className="-ml-1 size-7" />
          <Separator
            orientation="vertical"
            className="mr-1 data-vertical:h-4 data-vertical:self-auto"
          />
          <ConsoleBreadcrumbs />
        </header>
        <div className="fabric-page-content min-h-0 flex-1 overflow-auto">
          {children}
        </div>
      </SidebarInset>
    </SidebarProvider></div>
  )
}
