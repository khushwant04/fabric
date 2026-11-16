import Link from "next/link"
import type { ReactNode } from "react"
import { AlertTriangleIcon, BookOpenIcon, BotIcon, CheckCircle2Icon, CpuIcon, FlaskConicalIcon, GlobeIcon, KeyRoundIcon, RepeatIcon, RocketIcon, ServerIcon, WaypointsIcon, WrenchIcon } from "lucide-react"

import { CreateDeploymentDrawer } from "@/components/console/deployment-form"
import { GatewayUrl } from "@/components/console/edge-gateway"
import { LiveRefresh } from "@/components/console/live-refresh"
import { PageContainer } from "@/components/console/page-header"
import { IconTile, Metric, MetricGrid, Section, SectionEmpty, SectionLink, labelClass } from "@/components/console/section"
import { AccountMetricsProvider, OverviewSparkline } from "@/components/console/serving-metrics/overview-sparkline"
import { StatusBadge, statusTone } from "@/components/console/status-badge"
import { TruncatedText } from "@/components/console/truncated-text"
import { Greeting } from "@/components/dashboard/greeting"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Button } from "@/components/ui/button"
import { describeAccelerators } from "@/lib/fabric/accelerators"
import type { DeploymentStampChoice } from "@/lib/fabric/model-catalog"
import type { AccountUsage, Deployment, Stamp } from "@/lib/fabric/types"
import { formatNumber, formatRelative } from "@/lib/format"

type State = { status: string; detail: string }

export type DashboardOverviewProps = {
  checkedAt: string
  error: string | null
  /** The signed-in person's first name, for the greeting. */
  firstName?: string | null
  canCreate: boolean
  /** Active stamps offered by the create-deployment drawer. */
  stampOptions: DeploymentStampChoice[]
  /** The stamp list could not be read, so the drawer blocks deploying. */
  infrastructureUnavailable: boolean
  /** Null when the account's deployments could not be read. */
  deployments: { total: number; ready: number; attention: number; phases: Array<[string, number]>; recent: Array<{ deployment: Deployment; state: State }> } | null
  stamps: { total: number; active: number; rows: Array<{ stamp: Stamp; state: State }> } | null
  /** Null unless every active stamp reported its GPUs. */
  capacity: { used: number; total: number } | null
  usage: AccountUsage | null
  /** The edge gateway, when the console is configured with one. */
  edge?: { url: string; usedByConsole: boolean } | null
  /** Whether the account has an API key; null when it could not be checked. */
  hasApiKey?: boolean | null
}

const DOCS = "https://github.com/khushwant04/fabric/blob/main/docs"

const learn: Array<{ href: string; title: string; description: string; icon: ReactNode }> = [
  { href: `${DOCS}/agent-runs.md`, title: "Agent runs and traces", description: "Budgets, step traces and the tool-call guard.", icon: BotIcon },
  { href: `${DOCS}/agent-tool-loop.md`, title: "Server-side tool loop", description: "Let the gateway run tool calls until the task is done.", icon: RepeatIcon },
  { href: `${DOCS}/hosted-tools.md`, title: "Hosted tools", description: "Code interpreter, web fetch and web search.", icon: WrenchIcon },
  { href: `${DOCS}/openai-agents-sdk.md`, title: "OpenAI Agents SDK", description: "Point the SDK at Fabric and keep your agent code.", icon: BookOpenIcon },
  { href: `${DOCS}/edge-gateway.md`, title: "Edge gateway", description: "One inference URL in front of every stamp.", icon: GlobeIcon },
  { href: `${DOCS}/a10-k3s-installation.md`, title: "Set up a GPU VM", description: "Install a k3s stamp on an A10 virtual machine.", icon: CpuIcon },
].map((item) => ({ ...item, icon: <item.icon /> }))

const toneFill: Record<string, string> = {
  good: "bg-status-good",
  warning: "bg-status-warning",
  serious: "bg-status-serious",
  critical: "bg-status-critical",
  info: "bg-info",
  neutral: "bg-muted-foreground/50",
}

/** Serving phases as one segmented bar, so the mix reads at a glance. */
function PhaseBar({ phases }: { phases: Array<[string, number]> }) {
  const total = phases.reduce((sum, [, count]) => sum + count, 0)
  if (!total) return <div className="h-1.5 rounded-full bg-muted" />
  return <div className="flex h-1.5 gap-0.5 overflow-hidden rounded-full" role="img" aria-label={phases.map(([phase, count]) => `${count} ${phase.replaceAll("_", " ")}`).join(", ")}>
    {phases.map(([phase, count]) => <span key={phase} className={toneFill[statusTone(phase)] ?? toneFill.neutral} style={{ width: `${(count / total) * 100}%` }} />)}
  </div>
}

function UsageBar({ used, total }: { used: number; total: number }) {
  const percent = total ? Math.min(100, Math.round((used / total) * 100)) : 0
  return <div className="h-1.5 overflow-hidden rounded-full bg-muted" role="meter" aria-valuemin={0} aria-valuemax={total} aria-valuenow={used} aria-label="GPUs requested">
    <div className="h-full rounded-full bg-foreground" style={{ width: `${percent}%` }} />
  </div>
}

function stampGpus(stamp: Stamp): string | null {
  return describeAccelerators(stamp.capabilities)
}

/** Something that needs the operator: a tinted icon, one sentence and a link. */
function AttentionCard({ tone, icon, children, href, link }: { tone: "brand" | "warning" | "good" | "neutral"; icon: ReactNode; children: ReactNode; href: string; link: string }) {
  return <div className="flex min-w-0 items-start gap-4 rounded-2xl border border-border bg-card p-5 shadow-[0_1px_2px_rgb(0_0_0/0.025)]">
    <IconTile tone={tone}>{icon}</IconTile>
    <div className="flex min-w-0 flex-1 flex-col items-start gap-2">
      <p className="text-[15px] leading-6">{children}</p>
      <SectionLink href={href}>{link}</SectionLink>
    </div>
  </div>
}

function QuickStart({ icon, title, description, action }: { icon: ReactNode; title: string; description: string; action: ReactNode }) {
  return <div className="flex min-w-0 flex-col gap-3 rounded-2xl border border-border bg-card p-6 shadow-[0_1px_2px_rgb(0_0_0/0.025)]">
    <div className="flex items-center gap-3"><IconTile tone="neutral">{icon}</IconTile><h3 className="text-base font-medium">{title}</h3></div>
    <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">{description}</p>
    <div className="mt-auto flex justify-end pt-1">{action}</div>
  </div>
}

// The console's home: a greeting, what needs attention, three ways to start, the account
// at a glance (headline numbers, recent deployments, stamps) and links to the guides.
export function DashboardOverview({ checkedAt, error, firstName = null, canCreate, stampOptions, infrastructureUnavailable, deployments, stamps, capacity, usage, edge = null, hasApiKey = null }: DashboardOverviewProps) {
  const ready = deployments?.ready ?? 0
  const attention = deployments?.attention ?? 0
  const recent = deployments?.recent ?? []
  const phases = deployments?.phases ?? []
  const stampRows = stamps?.rows ?? []
  const activeStamps = stamps?.active ?? 0
  const unavailable = "—"

  const notices: ReactNode[] = []
  if (stamps !== null && activeStamps === 0) {
    notices.push(<AttentionCard key="stamps" tone="brand" icon={<ServerIcon />} href="/stamps" link="Enroll a stamp">No inference stamp is connected yet. Enroll a Kubernetes cluster with GPUs so Fabric has somewhere to run models.</AttentionCard>)
  }
  if (deployments === null) {
    notices.push(<AttentionCard key="deployments-unavailable" tone="warning" icon={<AlertTriangleIcon />} href="/deployments" link="Open deployments">Deployment data could not be checked. Refresh to try again.</AttentionCard>)
  } else if (attention) {
    notices.push(<AttentionCard key="review" tone="warning" icon={<AlertTriangleIcon />} href="/deployments" link="Review deployments">{attention} deployment{attention === 1 ? " needs" : "s need"} review: waiting for capacity, degraded or failed.</AttentionCard>)
  } else if (deployments.total === 0) {
    notices.push(<AttentionCard key="first" tone="brand" icon={<RocketIcon />} href="/deployments/new" link="Deploy a model">No models are deployed yet. Pick one and Fabric places it on a stamp with room for it.</AttentionCard>)
  }
  if (hasApiKey === false) {
    notices.push(<AttentionCard key="key" tone="brand" icon={<KeyRoundIcon />} href="/access/api-keys" link="Create an API key">This account has no API key yet. Keys are exchanged for short-lived inference tokens.</AttentionCard>)
  }
  if (!notices.length && deployments && deployments.total) {
    notices.push(<AttentionCard key="ok" tone="good" icon={<CheckCircle2Icon />} href="/deployments" link="View deployments">{ready === deployments.total ? `All ${deployments.total} deployments are serving.` : `${ready} of ${deployments.total} deployments are serving; the rest are rolling out.`}</AttentionCard>)
  }

  // One notice leaves half the row empty; the agent playground fills it once models serve.
  if (notices.length === 1 && ready > 0) {
    notices.push(<AttentionCard key="agents" tone="brand" icon={<BotIcon />} href="/agents/playground" link="Open the agent playground">Run a tool-using agent on your deployed models and watch every step it takes.</AttentionCard>)
  }

  const noStamps = stamps !== null && activeStamps === 0

  return <PageContainer>
    <div className="flex flex-col gap-1 pt-2">
      <Greeting firstName={firstName} />
      <p className="text-[15px] text-muted-foreground">Deployments, GPU capacity and inference traffic for this account.</p>
    </div>

    {notices.length ? <div className="grid gap-4 md:grid-cols-2">{notices.slice(0, 2)}</div> : null}

    <section className="flex flex-col gap-3" aria-labelledby="quick-start">
      <h2 id="quick-start" className={labelClass}>Quick start</h2>
      <div className="grid gap-4 md:grid-cols-3">
        <QuickStart icon={<FlaskConicalIcon />} title="Playground" description="Compare two deployed models side by side: answers, speed and routing." action={<Button nativeButton={false} render={<Link href="/playground" />}>Open playground</Button>} />
        <QuickStart icon={<RocketIcon />} title="Deploy a model" description="Pick a model; Fabric places it on a GPU stamp with room for it." action={canCreate ? <CreateDeploymentDrawer stamps={stampOptions} infrastructureUnavailable={infrastructureUnavailable} label="Deploy" icon={false} /> : <Button nativeButton={false} render={<Link href="/deployments" />}>View deployments</Button>} />
        {noStamps
          ? <QuickStart icon={<ServerIcon />} title="Enroll a stamp" description="Connect a GPU cluster; its agent reports capacity and runs models." action={<Button nativeButton={false} render={<Link href="/stamps" />}>Enroll stamp</Button>} />
          : <QuickStart icon={<KeyRoundIcon />} title="Create API key" description="Scoped keys for apps and service principals to call your models." action={<Button nativeButton={false} render={<Link href="/access/api-keys" />}>Create key</Button>} />}
      </div>
    </section>

    <section className="flex flex-col gap-3" aria-labelledby="at-a-glance">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="at-a-glance" className={labelClass}>At a glance</h2>
        <div className="min-w-0 sm:ml-auto"><LiveRefresh checkedAt={checkedAt} error={error} /></div>
      </div>
      <MetricGrid className="sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Deployments serving" value={deployments === null ? unavailable : <>{ready}<span className="text-muted-foreground">/{deployments.total}</span></>} detail={deployments === null ? "Could not be checked" : `${attention} need review`}>
          <PhaseBar phases={phases} />
        </Metric>
        <Metric label="GPUs requested" value={capacity ? <>{capacity.used}<span className="text-muted-foreground">/{capacity.total}</span></> : unavailable} detail={capacity ? `${capacity.total ? Math.round(capacity.used / capacity.total * 100) : 0}% of account-owned capacity` : "Not every active stamp has reported GPUs"}>
          {capacity ? <UsageBar used={capacity.used} total={capacity.total} /> : <div className="h-1.5 rounded-full bg-muted" />}
        </Metric>
        <AccountMetricsProvider enabled={deployments !== null}>
          <Metric label="Requests" value={usage ? formatNumber(usage.events) : unavailable} detail={usage ? usage.last_occurred_at ? `Last request ${formatRelative(usage.last_occurred_at)}` : "No usage reported yet" : "Could not be checked"}>
            <OverviewSparkline field="requests_per_min" />
          </Metric>
          <Metric label="Tokens" value={usage ? formatNumber(usage.input_tokens + usage.output_tokens) : unavailable} detail={usage ? `${formatNumber(usage.input_tokens)} in · ${formatNumber(usage.output_tokens)} out` : "Could not be checked"}>
            <OverviewSparkline field="output_tokens_per_s" />
          </Metric>
        </AccountMetricsProvider>
      </MetricGrid>
      {edge ? <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-border bg-card px-5 py-3.5 shadow-[0_1px_2px_rgb(0_0_0/0.025)]">
        <span className="flex shrink-0 items-center gap-2 text-sm text-muted-foreground"><WaypointsIcon className="size-4" />Inference endpoint</span>
        <GatewayUrl url={edge.url} compact />
      </div> : null}
    </section>

    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <Section title="Recent deployments" action={<SectionLink href="/deployments">All deployments</SectionLink>}>
        {recent.length ? <Table>
          <TableHeader><TableRow><TableHead>Deployment</TableHead><TableHead>Status</TableHead><TableHead className="hidden md:table-cell">Capacity</TableHead><TableHead className="text-right">Updated</TableHead></TableRow></TableHeader>
          <TableBody>{recent.map(({ deployment, state }) => <TableRow key={deployment.id}>
            <TableCell><Link href={`/deployments/${deployment.id}`} className="block max-w-56 font-medium hover:underline"><TruncatedText text={deployment.name} /></Link><TruncatedText text={deployment.model_alias} className="max-w-56 font-mono text-xs text-muted-foreground" /></TableCell>
            <TableCell title={state.detail}><StatusBadge status={state.status} /></TableCell>
            <TableCell className="hidden text-muted-foreground md:table-cell">{deployment.desired_spec.replicas} replica{deployment.desired_spec.replicas === 1 ? "" : "s"} · {deployment.desired_spec.resources.gpu_class.toUpperCase()}</TableCell>
            <TableCell className="text-right text-muted-foreground tabular-nums">{formatRelative(deployment.updated_at)}</TableCell>
          </TableRow>)}</TableBody>
        </Table> : <SectionEmpty action={canCreate && deployments ? <Button variant="outline" nativeButton={false} render={<Link href="/deployments/new" />}>Deploy a model</Button> : undefined}>{deployments === null ? "Deployment data is unavailable. Refresh to check again." : "No deployments yet."}</SectionEmpty>}
      </Section>

      <Section title="Stamps" action={<SectionLink href="/stamps">Manage</SectionLink>}>
        {stampRows.length ? <ul className="border-t border-border">
          {stampRows.map(({ stamp, state }) => {
            const gpus = stampGpus(stamp)
            return <li key={stamp.id} className="flex items-start gap-3 border-b border-border px-6 py-4 last:border-b-0">
              <IconTile tone="neutral" className="size-8 rounded-lg [&_svg]:size-4"><ServerIcon /></IconTile>
              <span className="min-w-0 flex-1">
                <span className="flex items-center justify-between gap-2"><span className="truncate text-sm font-medium">{stamp.name}</span><StatusBadge status={state.status} /></span>
                <span className="mt-1 block truncate text-[13px] text-muted-foreground">{gpus ?? "No GPUs reported"}</span>
                <span className="mt-0.5 block truncate text-[13px] text-muted-foreground">{[stamp.region, stamp.orchestrator].filter(Boolean).join(" · ") || "Location unavailable"} · heartbeat {formatRelative(stamp.last_heartbeat_at)}</span>
              </span>
            </li>
          })}
        </ul> : <SectionEmpty action={stamps !== null ? <Button variant="outline" nativeButton={false} render={<Link href="/stamps" />}>Enroll a stamp</Button> : undefined}>{stamps === null ? "Stamp data is unavailable." : "No account-owned stamps are registered."}</SectionEmpty>}
      </Section>
    </div>

    <section className="flex flex-col gap-3" aria-labelledby="learn">
      <h2 id="learn" className={labelClass}>Learn</h2>
      <ul className="grid gap-x-6 gap-y-1 rounded-2xl border border-border bg-card p-3 shadow-[0_1px_2px_rgb(0_0_0/0.025)] md:grid-cols-2">
        {learn.map((item) => <li key={item.href}>
          <a href={item.href} target="_blank" rel="noreferrer" className="group flex items-center gap-3.5 rounded-xl p-3 outline-none hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring">
            <IconTile>{item.icon}</IconTile>
            <span className="min-w-0">
              <span className="block text-sm font-medium group-hover:underline group-hover:underline-offset-4">{item.title}</span>
              <span className="block truncate text-[13px] text-muted-foreground">{item.description}</span>
            </span>
          </a>
        </li>)}
      </ul>
    </section>
  </PageContainer>
}
