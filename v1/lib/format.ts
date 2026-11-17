export function formatNumber(value: number) {
  return new Intl.NumberFormat("en", { notation: value >= 1_000_000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value)
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "Never"
  return new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))
}

export function formatRelative(value: string | null | undefined) {
  if (!value) return "Never"
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000)
  const absolute = Math.abs(seconds)
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" })
  if (absolute < 60) return formatter.format(seconds, "second")
  if (absolute < 3600) return formatter.format(Math.round(seconds / 60), "minute")
  if (absolute < 86400) return formatter.format(Math.round(seconds / 3600), "hour")
  return formatter.format(Math.round(seconds / 86400), "day")
}

export function shortId(value: string) {
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
}

const ORCHESTRATORS: Record<string, string> = {
  aks: "AKS", eks: "EKS", gke: "GKE", k3s: "k3s", k8s: "Kubernetes", kubernetes: "Kubernetes", openshift: "OpenShift",
}

/** An orchestrator as people write it ("AKS", "GKE", "k3s"), not the agent's lowercase id. */
export function orchestratorLabel(value: string | null | undefined) {
  if (!value) return "Unknown"
  return ORCHESTRATORS[value.toLowerCase()] ?? value.charAt(0).toUpperCase() + value.slice(1)
}
