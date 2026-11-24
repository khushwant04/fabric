"use client"

import { CheckIcon, CopyIcon } from "lucide-react"

import { useCopyFeedback } from "@/components/ai-elements/use-copy-feedback"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export function CopyCode({ code, label, className }: { code: string; label: string; className?: string }) {
  const { copied, copy } = useCopyFeedback()
  return <div className={cn("overflow-hidden rounded-md border bg-muted/30", className)}>
    <div className="flex items-center justify-between gap-2 border-b px-3 py-1.5">
      <span className="text-[11px] font-medium text-muted-foreground">{label}</span>
      <Button type="button" variant="ghost" size="sm" className="h-7 gap-1.5 px-2 text-xs" onClick={() => { void copy(code) }} aria-label={`Copy ${label}`}>
        {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}{copied ? "Copied" : "Copy"}
      </Button>
    </div>
    <pre className="overflow-x-auto px-4 py-3 text-left font-mono text-[12px] leading-5"><code>{code}</code></pre>
  </div>
}
