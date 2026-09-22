"use client"

import * as React from "react"

type Theme = "light" | "dark" | "system"
type ResolvedTheme = "light" | "dark"
type ThemeSnapshot = { theme: Theme; resolvedTheme: ResolvedTheme }

const THEME_STORAGE_KEY = "fabric-theme"
const DARK_QUERY = "(prefers-color-scheme: dark)"
const serverSnapshot: ThemeSnapshot = {
  theme: "system",
  resolvedTheme: "light",
}

let currentTheme: Theme | null = null
let snapshot: ThemeSnapshot | null = null
const listeners = new Set<() => void>()
let mediaQuery: MediaQueryList | null = null

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

function getCurrentTheme(): Theme {
  currentTheme ??= readStoredTheme()
  return currentTheme
}

function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme !== "system") {
    return theme
  }

  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light"
}

function applyTheme(theme: Theme) {
  const resolvedTheme = resolveTheme(theme)
  document.documentElement.classList.toggle("dark", resolvedTheme === "dark")
  document.documentElement.style.colorScheme = resolvedTheme
}

function getSnapshot(): ThemeSnapshot {
  const theme = getCurrentTheme()
  const resolvedTheme = resolveTheme(theme)

  if (
    !snapshot ||
    snapshot.theme !== theme ||
    snapshot.resolvedTheme !== resolvedTheme
  ) {
    snapshot = { theme, resolvedTheme }
  }

  return snapshot
}

function notify() {
  snapshot = null
  listeners.forEach((listener) => listener())
}

function handleMediaChange() {
  if (getCurrentTheme() !== "system") {
    return
  }

  applyTheme("system")
  notify()
}

function handleStorageChange(event: StorageEvent) {
  if (event.key !== null && event.key !== THEME_STORAGE_KEY) {
    return
  }

  currentTheme = isTheme(event.newValue) ? event.newValue : "system"
  applyTheme(currentTheme)
  notify()
}

function subscribe(onStoreChange: () => void) {
  listeners.add(onStoreChange)

  if (listeners.size === 1) {
    mediaQuery = window.matchMedia(DARK_QUERY)
    mediaQuery.addEventListener("change", handleMediaChange)
    window.addEventListener("storage", handleStorageChange)
  }

  return () => {
    listeners.delete(onStoreChange)
    if (listeners.size === 0) {
      mediaQuery?.removeEventListener("change", handleMediaChange)
      mediaQuery = null
      window.removeEventListener("storage", handleStorageChange)
    }
  }
}

function setTheme(theme: Theme) {
  currentTheme = theme

  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // Keep the in-memory preference for this document when storage is blocked.
  }

  applyTheme(theme)
  notify()
}

function getServerSnapshot(): ThemeSnapshot {
  return serverSnapshot
}

export function ThemeSync() {
  useTheme()
  return null
}

export function useTheme() {
  const current = React.useSyncExternalStore(
    subscribe,
    getSnapshot,
    getServerSnapshot
  )

  return { ...current, setTheme }
}

export { isTheme, THEME_STORAGE_KEY }
export type { ResolvedTheme, Theme }
