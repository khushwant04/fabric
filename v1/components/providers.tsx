"use client"

import type { ReactNode } from "react"
import { ThemeProvider } from "next-themes"

import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { THEME_STORAGE_KEY } from "@/hooks/use-theme"

export function Providers({ children }: { children: ReactNode }) {
  return <ThemeProvider attribute="class" storageKey={THEME_STORAGE_KEY} defaultTheme="system" enableSystem>
    <TooltipProvider>{children}<Toaster /></TooltipProvider>
  </ThemeProvider>
}
