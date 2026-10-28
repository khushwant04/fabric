import { CircleIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

const healthy = new Set(["active", "ready", "healthy", "enabled"])
const warning = new Set(["pending", "deploying", "registered", "updating", "waiting", "unverified", "terminating", "deleting", "draining", "stale"])
const danger = new Set(["degraded", "failed", "error", "blocked", "revoked", "inactive", "offline"])

export function StatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase()
  return (
    <Badge variant="outline" className="ui-caption gap-1.5 bg-card capitalize">
      <CircleIcon
        className={cn(
          "size-2! fill-current",
          // Outcome colours from the design system, always beside a word.
          healthy.has(normalized) && "text-success",
          warning.has(normalized) && "text-warning",
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
