import { Status, type StatusTone } from "@/components/hexel/status"

// One map from Fabric's resource states to the design system's status tones, reused by
// every page. State is a dot beside a plain label, never a coloured pill.
const tones: Record<string, StatusTone> = {
  active: "good",
  ready: "good",
  healthy: "good",
  enabled: "good",
  pending: "info",
  deploying: "info",
  registered: "info",
  updating: "info",
  terminating: "neutral",
  deleting: "neutral",
  deleted: "neutral",
  draining: "neutral",
  waiting: "warning",
  unverified: "warning",
  stale: "warning",
  degraded: "serious",
  failed: "critical",
  error: "critical",
  blocked: "critical",
  revoked: "critical",
  inactive: "critical",
  offline: "critical",
}

export function statusTone(status: string): StatusTone {
  return tones[status.toLowerCase()] ?? "neutral"
}

export function StatusBadge({ status }: { status: string }) {
  const label = status.replaceAll("_", " ")
  return <Status tone={statusTone(status)}>{label.charAt(0).toUpperCase() + label.slice(1)}</Status>
}
