import { DashboardOverview } from "@/components/dashboard/overview"
import { getAccountUsage, getDeploymentStatuses, listDeployments, listPlacements, listStamps } from "@/lib/fabric/data"
import { deploymentState, gpuCapacity, stampState } from "@/lib/fabric/resource-state"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"

function timestamp(value: string | null) {
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isNaN(parsed) ? 0 : parsed
}

export default async function DashboardPage() {
  const context = await getConsoleContext()
  const canReadDeployments = hasScope(context, "deployments:read")
  const canReadStamps = hasScope(context, "stamps:read")
  const [deploymentResult, stampResult, usageResult] = await Promise.allSettled([
    canReadDeployments ? listDeployments() : Promise.resolve(null),
    canReadStamps ? listStamps() : Promise.resolve(null),
    canReadDeployments ? getAccountUsage() : Promise.resolve(null),
  ])
  const deploymentData = deploymentResult.status === "fulfilled" ? deploymentResult.value : null
  const stampData = stampResult.status === "fulfilled" ? stampResult.value : null
  const usage = usageResult.status === "fulfilled" ? usageResult.value : null
  const deployments = deploymentData ?? []
  const stamps = stampData ?? []
  const observed = []
  for (let index = 0; index < deployments.length; index += 4) {
    observed.push(...await Promise.all(deployments.slice(index, index + 4).map(async (deployment) => {
      try {
        const [statuses, placements] = await Promise.all([getDeploymentStatuses(deployment.id), listPlacements(deployment.id)])
        return { deployment, state: deploymentState(deployment, statuses, placements, stampData ?? undefined), failed: false }
      } catch {
        return { deployment, state: deploymentState(deployment), failed: true }
      }
    })))
  }
  const error = [deploymentResult, stampResult, usageResult].some((result) => result.status === "rejected") || observed.some((item) => item.failed)
    ? "Some account data could not be checked." : null
  const checkedAt = new Date().toISOString()
  const ready = observed.filter((item) => item.state.status === "ready").length
  const attention = observed.filter((item) => ["degraded", "failed", "error", "unverified", "waiting"].includes(item.state.status)).length
  const phases = Object.entries(observed.reduce<Record<string, number>>((result, item) => {
    result[item.state.status] = (result[item.state.status] ?? 0) + 1
    return result
  }, {})).sort(([, left], [, right]) => right - left)
  const stampRows = stamps.map((stamp) => ({ stamp, state: stampState(stamp) }))
  const activeStamps = stampRows.filter((item) => item.state.status === "active")
  const capacityKnown = stampData !== null && activeStamps.length > 0 && activeStamps.every(({ stamp }) => gpuCapacity(stamp.capabilities.allocatable_gpus) !== null && gpuCapacity(stamp.capabilities.requested_gpus) !== null)
  const totalGpu = activeStamps.reduce((sum, { stamp }) => sum + (gpuCapacity(stamp.capabilities.allocatable_gpus) ?? 0), 0)
  const usedGpu = activeStamps.reduce((sum, { stamp }) => sum + (gpuCapacity(stamp.capabilities.requested_gpus) ?? 0), 0)
  const recent = [...observed].sort((left, right) => timestamp(right.deployment.updated_at) - timestamp(left.deployment.updated_at))
  const canCreate = hasScope(context, "deployments:write")

  return <DashboardOverview
    checkedAt={checkedAt}
    error={error}
    canCreate={canCreate}
    deployments={deploymentData === null ? null : { total: deployments.length, ready, attention, phases, recent: recent.slice(0, 5) }}
    stamps={stampData === null ? null : { total: stamps.length, active: activeStamps.length, rows: stampRows.slice(0, 3) }}
    capacity={capacityKnown ? { used: usedGpu, total: totalGpu } : null}
    usage={usage}
  />
}
