"use client"

import { useRef, useState } from "react"
import { cn } from "cn"

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

/**
 * One line of text cut with an ellipsis at its container's width. Hovering text that is
 * actually cut shows the whole value in a tooltip; text that fits shows none. The full
 * value stays in the DOM, so assistive technology and copy both get all of it.
 */
export function TruncatedText({ text, className }: { text: string; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [open, setOpen] = useState(false)
  return <Tooltip open={open} onOpenChange={(next) => {
    const element = ref.current
    setOpen(next && !!element && element.scrollWidth > element.clientWidth)
  }}>
    <TooltipTrigger render={<span ref={ref} data-slot="truncated-text" className={cn("block min-w-0 truncate", className)} />}>{text}</TooltipTrigger>
    <TooltipContent className="max-w-md break-all">{text}</TooltipContent>
  </Tooltip>
}
