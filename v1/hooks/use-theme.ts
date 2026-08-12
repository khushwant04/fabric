"use client"

import * as React from "react"

type Theme = "light" | "dark" | "system"
type ResolvedTheme = "light" | "dark"

const THEME_STORAGE_KEY = "fabric-theme"
const DARK_QUERY = "(prefers-color-scheme: dark)"

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark" || value === "system"
}

function readStoredTheme(): Theme {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isTheme(stored) ? stored : "system"
  } catch {
    return "system"
  }
}

function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme !== "system") {
    return theme
  }

  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light"
}

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle(
    "dark",
    resolveTheme(theme) === "dark"
  )
}

const listeners = new Set<() => void>()

function subscribe(onStoreChange: () => void) {
  const mediaQuery = window.matchMedia(DARK_QUERY)

  // Re-apply on OS scheme change so "system" stays accurate, and on
  // localStorage writes from other tabs.
  const handleExternalChange = () => {
    applyTheme(readStoredTheme())
    onStoreChange()
  }

  listeners.add(onStoreChange)
  mediaQuery.addEventListener("change", handleExternalChange)
  window.addEventListener("storage", handleExternalChange)

  return () => {
    listeners.delete(onStoreChange)
    mediaQuery.removeEventListener("change", handleExternalChange)
    window.removeEventListener("storage", handleExternalChange)
  }
}

function setTheme(theme: Theme) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // Ignore storage failures; the class change below still applies.
  }

  applyTheme(theme)
  listeners.forEach((listener) => listener())
}

function getThemeSnapshot(): Theme {
  return readStoredTheme()
}

function getResolvedThemeSnapshot(): ResolvedTheme {
  return resolveTheme(readStoredTheme())
}

// The inline script in the root layout applies the class before hydration,
// so the server snapshots only need to be stable defaults.
function getServerThemeSnapshot(): Theme {
  return "system"
}

function getServerResolvedThemeSnapshot(): ResolvedTheme {
  return "light"
}

export function useTheme() {
  const theme = React.useSyncExternalStore(
    subscribe,
    getThemeSnapshot,
    getServerThemeSnapshot
  )
  const resolvedTheme = React.useSyncExternalStore(
    subscribe,
    getResolvedThemeSnapshot,
    getServerResolvedThemeSnapshot
  )

  return { theme, resolvedTheme, setTheme }
}

export { THEME_STORAGE_KEY }
export type { ResolvedTheme, Theme }
