import Link from "next/link"
import {
  ActivityIcon,
  ArrowRightIcon,
  BoxesIcon,
  CpuIcon,
  GaugeIcon,
  PlusIcon,
  TriangleAlertIcon,
} from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge } from "@/components/console/status-badge"
import { PhaseChart } from "@/components/dashboard/phase-chart"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Progress } from "@/components/ui/progress"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  getAccountUsage,
  listDeployments,
  listStamps,
} from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatNumber, formatRelative } from "@/lib/format"

function timestamp(value: string | null) {
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isNaN(parsed) ? 0 : parsed
}

export default async function DashboardPage() {
  const [context, deployments, stamps, usage] = await Promise.all([
    getConsoleContext(),
    listDeployments(),
    listStamps(),
    getAccountUsage(),
  ])
  const totals = {
    events: usage.events,
    inputTokens: usage.input_tokens,
    outputTokens: usage.output_tokens,
  }
  const latestUsageAt = usage.last_occurred_at
  const ready = deployments.filter((item) =>
    ["ready", "active"].includes(item.status.toLowerCase())
  ).length
  const attention = deployments.filter((item) =>
    ["degraded", "failed", "error"].includes(item.status.toLowerCase())
  ).length
  const liveStamps = stamps.filter(
    (item) => item.status === "active" && !item.revoked_at
  ).length
  const phaseData = Object.entries(
    deployments.reduce<Record<string, number>>((result, item) => {
      result[item.status] = (result[item.status] ?? 0) + 1
      return result
    }, {})
  ).map(([phase, count]) => ({ phase, deployments: count }))
  const totalGpu = stamps.reduce(
    (sum, stamp) =>
      sum + Number(stamp.capabilities.allocatable_gpus ?? 0),
    0
  )
  const usedGpu = stamps.reduce(
    (sum, stamp) => sum + Number(stamp.capabilities.requested_gpus ?? 0),
    0
  )
  const recentDeployments = [...deployments].sort(
    (left, right) => timestamp(right.updated_at) - timestamp(left.updated_at)
  )
  const canCreateDeployment = hasScope(context, "deployments:write")
  const usageReported = latestUsageAt !== null

  return (
    <PageContainer>
      <PageHeader
        title="Overview"
        description="Monitor deployment health, connected infrastructure, and operational usage across this account."
        actions={
          canCreateDeployment ? (
            <Button render={<Link href="/deployments/new" />}>
              <PlusIcon /> Create deployment
            </Button>
          ) : undefined
        }
      />
      {attention > 0 ? (
        <Alert>
          <TriangleAlertIcon />
          <AlertTitle>
            {attention} deployment{attention === 1 ? "" : "s"} need attention
          </AlertTitle>
          <AlertDescription>
            Open deployment status to compare desired and observed generations.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card className="gap-0 py-0">
        <div className="grid divide-y sm:grid-cols-2 sm:divide-x sm:divide-y-0 xl:grid-cols-4">
          {[
            {
              label: "Deployments",
              value: deployments.length,
              meta: `${ready} ready`,
              icon: BoxesIcon,
            },
            {
              label: "Active stamps",
              value: liveStamps,
              meta: `${stamps.length} registered`,
              icon: CpuIcon,
            },
            {
              label: "Inference events",
              value: formatNumber(totals.events),
              meta: usageReported
                ? `Last reported ${formatRelative(latestUsageAt)}`
                : "Never reported",
              icon: ActivityIcon,
            },
            {
              label: "Tokens processed",
              value: formatNumber(totals.inputTokens + totals.outputTokens),
              meta: `${formatNumber(totals.inputTokens)} input · ${formatNumber(totals.outputTokens)} output`,
              icon: GaugeIcon,
            },
          ].map((metric) => (
            <div className="p-5" key={metric.label}>
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>{metric.label}</span>
                <metric.icon className="size-4" />
              </div>
              <div className="mt-3 text-2xl font-semibold tabular-nums">
                {metric.value}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                {metric.meta}
              </div>
            </div>
          ))}
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[1.55fr_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Deployment health</CardTitle>
            <CardDescription>
              Current control-plane phase by deployment.
            </CardDescription>
            <CardAction>
              <Button
                variant="ghost"
                size="sm"
                render={<Link href="/deployments" />}
              >
                View all <ArrowRightIcon />
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            {phaseData.length ? (
              <PhaseChart data={phaseData} />
            ) : (
              <div className="flex h-56 items-center justify-center text-sm text-muted-foreground">
                No deployment data yet.
              </div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>GPU capacity</CardTitle>
            <CardDescription>
              Reported capacity across account-owned stamps.
            </CardDescription>
            <CardAction>
              <Button
                variant="ghost"
                size="sm"
                render={<Link href="/stamps" />}
              >
                View all <ArrowRightIcon />
              </Button>
            </CardAction>
          </CardHeader>
          {stamps.length ? (
            <CardContent className="gap-5">
              <div>
                <div className="mb-2 flex justify-between text-sm">
                  <span>{usedGpu} GPU requested</span>
                  <span className="text-muted-foreground">
                    {totalGpu} allocatable
                  </span>
                </div>
                <Progress
                  value={totalGpu ? (usedGpu / totalGpu) * 100 : 0}
                />
              </div>
              <div className="divide-y rounded-lg border">
                {stamps.slice(0, 4).map((stamp) => {
                  const allocatable = Number(
                    stamp.capabilities.allocatable_gpus ?? 0
                  )
                  const requested = Number(
                    stamp.capabilities.requested_gpus ?? 0
                  )
                  return (
                    <div key={stamp.id} className="px-3 py-2.5">
                      <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium">
                            {stamp.name}
                          </div>
                          <div className="text-xs text-muted-foreground">
                            {stamp.region || "Region unavailable"}
                            {stamp.orchestrator
                              ? ` · ${stamp.orchestrator}`
                              : ""}
                          </div>
                        </div>
                        <StatusBadge
                          status={stamp.revoked_at ? "revoked" : stamp.status}
                        />
                      </div>
                      <div className="mt-2 flex justify-between text-xs text-muted-foreground">
                        <span>
                          {requested} of {allocatable} GPUs requested
                        </span>
                        <span>
                          Heartbeat {formatRelative(stamp.last_heartbeat_at)}
                        </span>
                      </div>
                    </div>
                  )
                })}
              </div>
              {stamps.length > 4 ? (
                <p className="text-xs text-muted-foreground">
                  Showing 4 of {stamps.length} registered stamps.
                </p>
              ) : null}
            </CardContent>
          ) : (
            <Empty className="min-h-56 border-0">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <CpuIcon />
                </EmptyMedia>
                <EmptyTitle>No stamps connected</EmptyTitle>
                <EmptyDescription>
                  Connect a Fabric agent to report GPU capacity and heartbeat
                  health.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent deployments</CardTitle>
          <CardDescription>
            Latest desired-state changes for this account.
          </CardDescription>
          {deployments.length ? (
            <CardAction>
              <Button
                variant="ghost"
                size="sm"
                render={<Link href="/deployments" />}
              >
                View all <ArrowRightIcon />
              </Button>
            </CardAction>
          ) : null}
        </CardHeader>
        {deployments.length ? (
          <CardContent className="px-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-6">Name</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Resources</TableHead>
                  <TableHead className="pr-6 text-right">Updated</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {recentDeployments.slice(0, 6).map((deployment) => (
                  <TableRow key={deployment.id}>
                    <TableCell className="pl-6 font-medium">
                      <Link
                        href={`/deployments/${deployment.id}`}
                        className="hover:underline"
                      >
                        {deployment.name}
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {deployment.model_alias}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={deployment.status} />
                    </TableCell>
                    <TableCell>
                      {deployment.desired_spec.replicas} replica
                      {deployment.desired_spec.replicas === 1 ? "" : "s"} ×{" "}
                      {deployment.desired_spec.resources.gpu_count} GPU
                      {deployment.desired_spec.resources.gpu_count === 1
                        ? ""
                        : "s"}{" "}
                      ({deployment.desired_spec.resources.gpu_class.toUpperCase()})
                    </TableCell>
                    <TableCell className="pr-6 text-right text-muted-foreground">
                      {formatRelative(deployment.updated_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {deployments.length > 6 ? (
              <p className="px-6 pt-4 text-xs text-muted-foreground">
                Showing 6 of {deployments.length} deployments by most recent
                update.
              </p>
            ) : null}
          </CardContent>
        ) : (
          <Empty className="min-h-64 border-0">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <BoxesIcon />
              </EmptyMedia>
              <EmptyTitle>No deployments yet</EmptyTitle>
              <EmptyDescription>
                Created deployments and their desired-state changes will appear
                here.
              </EmptyDescription>
            </EmptyHeader>
            {canCreateDeployment ? (
              <Button render={<Link href="/deployments/new" />}>
                <PlusIcon /> Create deployment
              </Button>
            ) : null}
          </Empty>
        )}
      </Card>
    </PageContainer>
  )
}
