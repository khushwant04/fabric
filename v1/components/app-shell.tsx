"use client"

import * as React from "react"

import { AppHeader } from "@/components/app-header"
import { AppSidebar } from "@/components/app-sidebar"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"

const user = {
  name: "shadcn",
  email: "m@example.com",
}

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <SidebarProvider>
      <div className="flex min-h-svh w-full flex-col overflow-hidden">
        <AppHeader user={user} />
        <div className="flex min-h-0 flex-1">
          <AppSidebar className="top-12 h-[calc(100svh-3rem)]" />
          <SidebarInset className="min-h-0 min-w-0 overflow-auto">
            {children}
          </SidebarInset>
        </div>
      </div>
    </SidebarProvider>
  )
}
