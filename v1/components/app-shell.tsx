"use client"

import type { ReactNode } from "react"
import Link from "next/link"

import { AppSidebar } from "@/components/app-sidebar"
import { FabricMark } from "@/components/fabric-mark"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import type { ConsoleContext } from "@/lib/fabric/types"

// One white sidebar beside a white main area. There is no top bar: the sidebar holds the
// brand, search, navigation and the account menu. Below the mobile breakpoint a slim bar
// with the menu button takes its place.
export function AppShell({
  children,
  context,
}: {
  children: ReactNode
  context: ConsoleContext
}) {
  return (
    <SidebarProvider
      className="fabric-layout"
      style={
        {
          "--sidebar-width": "17.5rem",
          "--sidebar-width-icon": "3.75rem",
        } as React.CSSProperties
      }
    >
      <AppSidebar context={context} />
      <SidebarInset className="fabric-inset" aria-label="Console content">
        <header className="flex h-14 shrink-0 items-center gap-2 border-b border-border px-4 md:hidden" aria-label="Mobile header">
          <SidebarTrigger className="-ml-1" />
          <Link href="/dashboard" aria-label="Fabric home" className="flex items-center gap-2 rounded-lg text-[17px] font-semibold tracking-[-0.03em] outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <FabricMark />Fabric
          </Link>
        </header>
        <div className="fabric-page-content min-h-0 flex-1 overflow-auto">
          {children}
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
