import Link from "next/link"
import { ActivityIcon, BoxesIcon, FlaskConicalIcon, KeyRoundIcon, PlusIcon } from "lucide-react"

import { LiveRefresh } from "@/components/console/live-refresh"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge, statusTone } from "@/components/console/status-badge"
import { PanelLink, PanelLinkRow } from "@/components/dashboard/surfaces"
import { BarList } from "@/components/hexel/bar-list"
import { Meter } from "@/components/hexel/meter"
import { Panel, PanelAction, PanelContent, PanelFooter, PanelHeader, PanelRow, PanelTitle } from "@/components/hexel/panel"
import { StatGroup, StatTile } from "@/components/hexel/stat-tile"
import { Status } from "@/components/hexel/status"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/hexel/table"
import { Button } from "@/components/ui/button"
import type { AccountUsage, Deployment, Stamp } from "@/lib/fabric/types"
import { formatNumber, formatRelative } from "@/lib/format"

type State = { status: string; detail: string }

export type DashboardOverviewProps = {
  checkedAt: string
  error: string | null
  canCreate: boolean
  /** Null when the account's deployments could not be read. */
  deployments: { total: number; ready: number; attention: number; phases: Array<[string, number]>; recent: Array<{ deployment: Deployment; state: State }> } | null
  stamps: { total: number; active: number; rows: Array<{ stamp: Stamp; state: State }> } | null
  /** Null unless every active stamp reported its GPUs. */
  capacity: { used: number; total: number } | null
  usage: AccountUsage | null
}

// The console's Overview, laid out as the Hexel Studio console sample: headline numbers in
// one StatGroup, then Panels (a tray carrying the title, a raised card the content).
export function DashboardOverview({ checkedAt, error, canCreate, deployments, stamps, capacity, usage }: DashboardOverviewProps) {
  const ready = deployments?.ready ?? 0
  const attention = deployments?.attention ?? 0
  const recent = deployments?.recent ?? []
  const phases = deployments?.phases ?? []
  const stampRows = stamps?.rows ?? []
  const unavailable = "Unavailable"
  return <PageContainer>
    <PageHeader
      title="Overview"
      description="Deployment health, GPU capacity and inference usage in one place."
      actions={<>
        <Button nativeButton={false} variant="outline" render={<Link href="/playground" />}><FlaskConicalIcon />Open playground</Button>
        {canCreate ? <Button nativeButton={false} render={<Link href="/deployments/new" />}><PlusIcon />Create deployment</Button> : null}
      </>}
    />

    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <Status tone={deployments === null ? "neutral" : ready === deployments.total && ready > 0 ? "good" : "neutral"}>
          {deployments === null ? "Deployment data unavailable" : `${ready} of ${deployments.total} deployments observed ready`}
        </Status>
        {attention ? <Link href="/deployments" className="rounded-md outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"><Status tone="warning">{attention} need{attention === 1 ? "s" : ""} review</Status></Link> : null}
      </div>
      <LiveRefresh checkedAt={checkedAt} error={error} />
    </div>

    <StatGroup>
      <StatTile label="Deployments" value={deployments === null ? unavailable : deployments.total} period={deployments === null ? "Could not be checked" : `${ready} observed ready`} />
      <StatTile label="Active stamps" value={stamps === null ? unavailable : stamps.active} period={stamps === null ? "Could not be checked" : `of ${stamps.total} registered`} />
      <StatTile label="Inference requests" value={usage ? formatNumber(usage.events) : unavailable} period={usage ? usage.last_occurred_at ? `Last ${formatRelative(usage.last_occurred_at)}` : "No usage reported yet" : "Could not be checked"} />
      <StatTile label="Tokens processed" value={usage ? formatNumber(usage.input_tokens + usage.output_tokens) : unavailable} period={usage ? `${formatNumber(usage.input_tokens)} in · ${formatNumber(usage.output_tokens)} out` : "Could not be checked"} />
    </StatGroup>

    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <Panel>
        <PanelHeader><PanelTitle>Recent deployments</PanelTitle><PanelAction><PanelLink href="/deployments">View all</PanelLink></PanelAction></PanelHeader>
        <PanelContent>
          {recent.length ? <Table>
            <TableHeader><TableRow><TableHead>Deployment</TableHead><TableHead>Status</TableHead><TableHead className="hidden md:table-cell">Capacity</TableHead><TableHead align="right">Updated</TableHead></TableRow></TableHeader>
            <TableBody>{recent.map(({ deployment, state }) => <TableRow key={deployment.id}>
              <TableCell className="h-14"><Link href={`/deployments/${deployment.id}`} className="font-medium hover:underline">{deployment.name}</Link><span className="block font-mono text-[11.5px] text-muted-foreground">{deployment.model_alias}</span></TableCell>
              <TableCell title={state.detail}><StatusBadge status={state.status} /></TableCell>
              <TableCell className="hidden text-muted-foreground md:table-cell">{deployment.desired_spec.replicas} replica{deployment.desired_spec.replicas === 1 ? "" : "s"} · {deployment.desired_spec.resources.gpu_class.toUpperCase()}</TableCell>
              <TableCell align="right" className="text-muted-foreground">{formatRelative(deployment.updated_at)}</TableCell>
            </TableRow>)}</TableBody>
          </Table> : <PanelRow className="flex-col gap-1 py-10 text-center">
            <BoxesIcon className="mb-2 size-5 text-muted-foreground" />
            <span className="font-medium">{deployments === null ? "Deployment data is unavailable" : "Your first deployment starts here"}</span>
            <span className="text-[13px] text-muted-foreground">{deployments === null ? "Refresh to check your account deployments again." : "Choose a model and let Fabric find compatible capacity."}</span>
          </PanelRow>}
        </PanelContent>
      </Panel>

      <Panel>
        <PanelHeader><PanelTitle>Deployment health</PanelTitle></PanelHeader>
        <PanelContent className="p-4">
          {phases.length
            ? <BarList items={phases.map(([phase, count]) => ({ key: phase, label: <Status tone={statusTone(phase)} className="capitalize">{phase.replaceAll("_", " ")}</Status>, value: count }))} />
            : <p className="py-6 text-center text-sm text-muted-foreground">{deployments === null ? "Deployment health is unavailable." : "Create a deployment to see its serving state."}</p>}
        </PanelContent>
        <PanelFooter>Observed serving state, from model-host reports at the current placement generation.</PanelFooter>
      </Panel>
    </div>

    <div className="grid items-start gap-4 lg:grid-cols-2">
      <Panel>
        <PanelHeader><PanelTitle>GPU capacity</PanelTitle><PanelAction><PanelLink href="/stamps">View stamps</PanelLink></PanelAction></PanelHeader>
        <PanelContent>
          <PanelRow>
            {capacity
              ? <Meter className="flex-1" value={capacity.used} max={capacity.total} label="Requested" detail={`${capacity.used} of ${capacity.total} GPUs · ${capacity.total ? Math.round(capacity.used / capacity.total * 100) : 0}%`} />
              : <span className="text-[13px] text-muted-foreground">Complete GPU capacity has not been reported by every active stamp.</span>}
          </PanelRow>
          {stampRows.length ? stampRows.map(({ stamp, state }) => <PanelRow key={stamp.id}>
            <span className="min-w-0 flex-1"><span className="block truncate font-medium">{stamp.name}</span><span className="block truncate text-[13px] text-muted-foreground">{stamp.region || "Region unavailable"} · Heartbeat {formatRelative(stamp.last_heartbeat_at)}</span></span>
            <StatusBadge status={state.status} />
          </PanelRow>) : <PanelRow className="justify-center text-muted-foreground">{stamps === null ? "Stamp data is unavailable." : "No account-owned stamps are registered."}</PanelRow>}
        </PanelContent>
        <PanelFooter>Account-owned stamps only; shared managed capacity is not included.</PanelFooter>
      </Panel>

      <Panel>
        <PanelHeader><PanelTitle>Explore your workspace</PanelTitle></PanelHeader>
        <PanelContent>
          <PanelLinkRow href="/playground" title="Inference playground" description="Compare two model responses, streaming speed and routing decisions." icon={<FlaskConicalIcon />} />
          <PanelLinkRow href="/access/api-keys" title="API credentials" description="Create scoped keys for applications and service principals." icon={<KeyRoundIcon />} />
          <PanelLinkRow href="/usage" title="Usage and activity" description="Review collector-reported request and token totals." icon={<ActivityIcon />} />
        </PanelContent>
        <PanelFooter>Usage is collector-reported by each stamp.</PanelFooter>
      </Panel>
    </div>
  </PageContainer>
}
