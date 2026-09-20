import { CircleIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

const healthy = new Set(["active", "ready", "healthy", "enabled"])
const warning = new Set(["pending", "deploying", "registered", "updating"])
const danger = new Set(["degraded", "failed", "error", "revoked", "inactive"])

export function StatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase()
  return (
    <Badge variant="outline" className="gap-1.5 bg-background font-normal capitalize">
      <CircleIcon
        className={cn(
          "size-2! fill-current",
          healthy.has(normalized) && "text-emerald-600",
          warning.has(normalized) && "text-amber-600",
          danger.has(normalized) && "text-destructive",
          !healthy.has(normalized) &&
            !warning.has(normalized) &&
            !danger.has(normalized) &&
            "text-muted-foreground"
        )}
      />
      {status.replaceAll("_", " ")}
    </Badge>
  )
}
