"use client"

import { useTheme as useNextTheme } from "next-themes"

type Theme = "light" | "dark" | "system"
type ResolvedTheme = "light" | "dark"
const THEME_STORAGE_KEY = "fabric-theme"

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark" || value === "system"
}

export function useTheme() {
  const current = useNextTheme()
  const theme: Theme = isTheme(current.theme) ? current.theme : "system"
  const resolvedTheme: ResolvedTheme = current.resolvedTheme === "dark" ? "dark" : "light"
  return { theme, resolvedTheme, setTheme: (value: Theme) => current.setTheme(value) }
}

export { isTheme, THEME_STORAGE_KEY }
export type { ResolvedTheme, Theme }
