import { LayersIcon } from "lucide-react"

import { cn } from "@/lib/utils"

/** The Fabric mark: layers in a near-black rounded tile. */
export function FabricMark({ className }: { className?: string }) {
  return <span aria-hidden="true" className={cn("flex size-7 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground", className)}>
    <LayersIcon className="size-4" strokeWidth={1.9} />
  </span>
}

/** The mark and the wordmark together. */
export function FabricLogo({ className }: { className?: string }) {
  return <span className={cn("flex items-center gap-2.5 text-[18px] font-semibold tracking-[-0.03em]", className)}><FabricMark />Fabric</span>
}
