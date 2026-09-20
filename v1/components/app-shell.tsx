"use client"

import type { ReactNode } from "react"

import { AppSidebar } from "@/components/app-sidebar"
import { ConsoleBreadcrumbs } from "@/components/console/breadcrumbs"
import { HeaderActions } from "@/components/app-header"
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
    <SidebarProvider
      style={
        {
          "--sidebar-width": "14.5rem",
        } as React.CSSProperties
      }
    >
      <AppSidebar context={context} />
      <SidebarInset className="min-w-0 overflow-hidden md:peer-data-[variant=inset]:rounded-lg md:peer-data-[variant=inset]:ring-1 md:peer-data-[variant=inset]:ring-border">
        <header className="sticky top-0 z-30 flex h-12 shrink-0 items-center gap-2 border-b bg-background px-3 sm:px-4">
          <SidebarTrigger className="-ml-1 size-7" />
          <Separator
            orientation="vertical"
            className="mr-1 data-vertical:h-4 data-vertical:self-auto"
          />
          <ConsoleBreadcrumbs />
          <HeaderActions context={context} />
        </header>
        <div className="min-h-0 flex-1 overflow-auto">
          {context.demo ? (
            <div className="border-b bg-muted/40 px-5 py-2 text-center text-xs text-muted-foreground">
              Preview data is enabled. Configure Auth0 and the control plane for
              live operations.
            </div>
          ) : null}
          {children}
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
