"use client"

import { useSyncExternalStore } from "react"

function partOfDay(hour: number) {
  if (hour < 5) return "Good evening"
  if (hour < 12) return "Good morning"
  if (hour < 18) return "Good afternoon"
  return "Good evening"
}

function subscribe(onChange: () => void) {
  const timer = window.setInterval(onChange, 60_000)
  return () => window.clearInterval(timer)
}

/** "Good evening, Maya", by the viewer's own clock; a neutral greeting while server-rendered. */
export function Greeting({ firstName }: { firstName: string | null }) {
  const hour = useSyncExternalStore(subscribe, () => new Date().getHours(), () => null)
  const greeting = hour === null ? "Welcome back" : partOfDay(hour)
  return <h1 className="text-[28px] leading-9 font-medium tracking-[-0.025em]">{firstName ? `${greeting}, ${firstName}` : greeting}</h1>
}
