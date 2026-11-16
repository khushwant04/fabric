import Link from "next/link"
import type { ReactNode } from "react"
import { ActivityIcon, ArrowRightIcon, CheckIcon, CpuIcon, FlaskConicalIcon, KeyRoundIcon, ServerIcon } from "lucide-react"

import { CreateDeploymentDrawer } from "@/components/console/deployment-form"
import { GatewayUrl } from "@/components/console/edge-gateway"
import { LiveRefresh } from "@/components/console/live-refresh"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge, statusTone } from "@/components/console/status-badge"
import { TruncatedText } from "@/components/console/truncated-text"
import { Status } from "@/components/hexel/status"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/hexel/table"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { DeploymentStampOption } from "@/lib/fabric/resource-state"
import type { AccountUsage, Deployment, Stamp } from "@/lib/fabric/types"
import { formatNumber, formatRelative } from "@/lib/format"

type State = { status: string; detail: string }

export type DashboardOverviewProps = {
  checkedAt: string
  error: string | null
  canCreate: boolean
  /** Active stamps offered by the create-deployment drawer. */
  stampOptions: DeploymentStampOption[]
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
}

const toneFill: Record<string, string> = {
  good: "bg-status-good",
  warning: "bg-status-warning",
  serious: "bg-status-serious",
  critical: "bg-status-critical",
  info: "bg-info",
  neutral: "bg-muted-foreground/50",
}

/** A square section with a hairline border and a compact label bar. */
function Section({ title, action, className, children }: { title: string; action?: ReactNode; className?: string; children: ReactNode }) {
  return <section className={cn("flex min-w-0 flex-col border border-border bg-card", className)}>
    <header className="flex h-10 shrink-0 items-center justify-between gap-3 border-b border-border px-4">
      <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h2>
      {action}
    </header>
    {children}
  </section>
}

function SectionLink({ href, children }: { href: string; children: ReactNode }) {
  return <Link href={href} className="inline-flex items-center gap-1 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:underline">
    {children}<ArrowRightIcon className="size-3" />
  </Link>
}

function Metric({ label, value, detail, children }: { label: string; value: ReactNode; detail: ReactNode; children?: ReactNode }) {
  return <div className="flex min-w-0 flex-col gap-2 bg-card p-4">
    <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{label}</span>
    <span className="font-heading text-3xl font-semibold tracking-tight tabular-nums">{value}</span>
    {children}
    <span className="truncate text-xs text-muted-foreground">{detail}</span>
  </div>
}

/** Serving phases as one segmented bar, so the mix reads at a glance. */
function PhaseBar({ phases }: { phases: Array<[string, number]> }) {
  const total = phases.reduce((sum, [, count]) => sum + count, 0)
  if (!total) return <div className="h-1.5 bg-muted" />
  return <div className="flex h-1.5 gap-px bg-muted" role="img" aria-label={phases.map(([phase, count]) => `${count} ${phase.replaceAll("_", " ")}`).join(", ")}>
    {phases.map(([phase, count]) => <span key={phase} className={toneFill[statusTone(phase)] ?? toneFill.neutral} style={{ width: `${(count / total) * 100}%` }} />)}
  </div>
}

function UsageBar({ used, total }: { used: number; total: number }) {
  const percent = total ? Math.min(100, Math.round((used / total) * 100)) : 0
  return <div className="h-1.5 bg-muted" role="meter" aria-valuemin={0} aria-valuemax={total} aria-valuenow={used} aria-label="GPUs requested">
    <div className="h-full bg-foreground" style={{ width: `${percent}%` }} />
  </div>
}

function stampGpus(stamp: Stamp): string | null {
  const gpus = stamp.capabilities?.gpus
  if (!Array.isArray(gpus) || !gpus.length) return null
  return gpus
    .map((gpu) => {
      const entry = gpu as { count?: number; product?: string }
      return `${entry.count ?? 1} × ${entry.product ?? "GPU"}`
    })
    .join(", ")
}

function Step({ done, index, title, children }: { done: boolean; index: number; title: string; children: ReactNode }) {
  return <li className="flex gap-4 border-b border-border px-4 py-4 last:border-b-0">
    <span className={cn("flex size-6 shrink-0 items-center justify-center border text-xs tabular-nums", done ? "border-foreground bg-foreground text-background" : "border-border text-muted-foreground")}>
      {done ? <CheckIcon className="size-3.5" /> : index}
    </span>
    <div className="flex min-w-0 flex-1 flex-col gap-2">
      <span className={cn("text-sm font-medium", done && "text-muted-foreground line-through decoration-1")}>{title}</span>
      {children}
    </div>
  </li>
}

function Shortcut({ href, icon, title, description }: { href: string; icon: ReactNode; title: string; description: string }) {
  return <Link href={href} className="group flex min-w-0 items-start gap-3 bg-card p-4 outline-none hover:bg-muted/60 focus-visible:bg-muted/60">
    <span className="flex size-8 shrink-0 items-center justify-center border border-border text-muted-foreground [&_svg]:size-4">{icon}</span>
    <span className="min-w-0 flex-1">
      <span className="flex items-center gap-1 text-sm font-medium">{title}<ArrowRightIcon className="size-3.5 opacity-0 transition-opacity group-hover:opacity-100" /></span>
      <span className="mt-0.5 block text-xs text-muted-foreground">{description}</span>
    </span>
  </Link>
}

// The console's home: a status strip, four headline metrics, then deployments beside the
// stamps that serve them. With no deployments yet, a short checklist takes the table's place.
export function DashboardOverview({ checkedAt, error, canCreate, stampOptions, infrastructureUnavailable, deployments, stamps, capacity, usage, edge = null }: DashboardOverviewProps) {
  const ready = deployments?.ready ?? 0
  const attention = deployments?.attention ?? 0
  const recent = deployments?.recent ?? []
  const phases = deployments?.phases ?? []
  const stampRows = stamps?.rows ?? []
  const unavailable = "—"
  const empty = deployments !== null && deployments.total === 0
  const createButton = canCreate ? <CreateDeploymentDrawer stamps={stampOptions} infrastructureUnavailable={infrastructureUnavailable} /> : null

  const health = deployments === null
    ? <Status>Deployment data unavailable</Status>
    : attention
      ? <Link href="/deployments" className="outline-none hover:underline focus-visible:underline"><Status tone="warning">{attention} deployment{attention === 1 ? "" : "s"} need{attention === 1 ? "s" : ""} review</Status></Link>
      : deployments.total
        ? <Status tone={ready === deployments.total ? "good" : "info"}>{ready === deployments.total ? "All deployments serving" : `${ready} of ${deployments.total} serving`}</Status>
        : <Status>No deployments yet</Status>

  return <PageContainer>
    <PageHeader
      title="Overview"
      description="Deployments, GPU capacity and inference traffic for this account."
      actions={<>
        <Button nativeButton={false} variant="outline" render={<Link href="/playground" />}><FlaskConicalIcon />Playground</Button>
        {createButton}
      </>}
    />

    <div className="grid gap-px border border-border bg-border lg:grid-cols-[auto_minmax(0,1fr)_auto]">
      <div className="flex items-center gap-4 bg-card px-4 py-3 text-sm">{health}</div>
      <div className="flex min-w-0 items-center gap-3 bg-card px-4 py-2">
        {edge
          ? <><span className="shrink-0 text-xs font-medium tracking-wide text-muted-foreground uppercase">Endpoint</span><GatewayUrl url={edge.url} compact /></>
          : <span className="text-sm text-muted-foreground">No edge gateway configured; each stamp serves its own URL.</span>}
      </div>
      <div className="flex items-center bg-card px-4 py-3"><LiveRefresh checkedAt={checkedAt} error={error} /></div>
    </div>

    <div className="grid gap-px border border-border bg-border sm:grid-cols-2 xl:grid-cols-4">
      <Metric label="Deployments" value={deployments === null ? unavailable : deployments.total} detail={deployments === null ? "Could not be checked" : `${ready} serving · ${attention} need review`}>
        <PhaseBar phases={phases} />
      </Metric>
      <Metric label="GPUs requested" value={capacity ? <>{capacity.used}<span className="text-muted-foreground">/{capacity.total}</span></> : unavailable} detail={capacity ? `${capacity.total ? Math.round(capacity.used / capacity.total * 100) : 0}% of account-owned capacity` : "Not every active stamp has reported GPUs"}>
        {capacity ? <UsageBar used={capacity.used} total={capacity.total} /> : <div className="h-1.5 bg-muted" />}
      </Metric>
      <Metric label="Requests" value={usage ? formatNumber(usage.events) : unavailable} detail={usage ? usage.last_occurred_at ? `Last request ${formatRelative(usage.last_occurred_at)}` : "No usage reported yet" : "Could not be checked"} />
      <Metric label="Tokens" value={usage ? formatNumber(usage.input_tokens + usage.output_tokens) : unavailable} detail={usage ? `${formatNumber(usage.input_tokens)} in · ${formatNumber(usage.output_tokens)} out` : "Could not be checked"} />
    </div>

    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      {empty
        ? <Section title="Get started">
          <ol>
            <Step index={1} done={(stamps?.active ?? 0) > 0} title="Connect a GPU stamp">
              <span className="text-xs text-muted-foreground">{(stamps?.active ?? 0) > 0 ? `${stamps?.active} active stamp${stamps?.active === 1 ? "" : "s"} reporting heartbeats.` : "Enroll a Kubernetes cluster with GPUs from Inference stamps."}</span>
            </Step>
            <Step index={2} done={false} title="Deploy a model">
              <span className="text-xs text-muted-foreground">Pick a model; Fabric places it on a stamp with room for it.</span>
              {createButton ? <div>{createButton}</div> : null}
            </Step>
            <Step index={3} done={false} title="Create an API key">
              <span className="text-xs text-muted-foreground">Keys are exchanged for short-lived inference tokens, so the key never reaches a gateway.</span>
              <div><Button nativeButton={false} variant="outline" size="sm" render={<Link href="/access/api-keys" />}><KeyRoundIcon />API keys</Button></div>
            </Step>
            <Step index={4} done={false} title="Call the endpoint">
              <span className="text-xs text-muted-foreground">Any OpenAI or Anthropic SDK works; set the base URL and the model name.</span>
              <code className="block overflow-x-auto border border-border bg-muted/50 px-3 py-2 font-mono text-xs whitespace-pre">{`curl ${edge?.url ?? "https://<inference-url>/v1"}/chat/completions \\\n  -H "Authorization: Bearer $TOKEN" \\\n  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}]}'`}</code>
            </Step>
          </ol>
        </Section>
        : <Section title="Deployments" action={<SectionLink href="/deployments">All deployments</SectionLink>}>
          {recent.length ? <Table>
            <TableHeader><TableRow><TableHead>Deployment</TableHead><TableHead>Status</TableHead><TableHead className="hidden md:table-cell">Capacity</TableHead><TableHead align="right">Updated</TableHead></TableRow></TableHeader>
            <TableBody>{recent.map(({ deployment, state }) => <TableRow key={deployment.id}>
              <TableCell className="h-14"><Link href={`/deployments/${deployment.id}`} className="block max-w-56 font-medium hover:underline"><TruncatedText text={deployment.name} /></Link><TruncatedText text={deployment.model_alias} className="max-w-56 font-mono text-[11.5px] text-muted-foreground" /></TableCell>
              <TableCell title={state.detail}><StatusBadge status={state.status} /></TableCell>
              <TableCell className="hidden text-muted-foreground md:table-cell">{deployment.desired_spec.replicas} replica{deployment.desired_spec.replicas === 1 ? "" : "s"} · {deployment.desired_spec.resources.gpu_class.toUpperCase()}</TableCell>
              <TableCell align="right" className="text-muted-foreground">{formatRelative(deployment.updated_at)}</TableCell>
            </TableRow>)}</TableBody>
          </Table> : <p className="px-4 py-10 text-center text-sm text-muted-foreground">Deployment data is unavailable. Refresh to check again.</p>}
        </Section>}

      <Section title="Stamps" action={<SectionLink href="/stamps">Manage</SectionLink>}>
        {stampRows.length ? <ul>
          {stampRows.map(({ stamp, state }) => {
            const gpus = stampGpus(stamp)
            return <li key={stamp.id} className="flex items-start gap-3 border-b border-border px-4 py-3 last:border-b-0">
              <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center border border-border text-muted-foreground"><ServerIcon className="size-4" /></span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center justify-between gap-2"><span className="truncate text-sm font-medium">{stamp.name}</span><StatusBadge status={state.status} /></span>
                <span className="mt-1 flex items-center gap-1.5 truncate text-xs text-muted-foreground"><CpuIcon className="size-3 shrink-0" />{gpus ?? "No GPUs reported"}</span>
                <span className="mt-0.5 block truncate text-xs text-muted-foreground">{[stamp.region, stamp.orchestrator].filter(Boolean).join(" · ") || "Location unavailable"} · heartbeat {formatRelative(stamp.last_heartbeat_at)}</span>
              </span>
            </li>
          })}
        </ul> : <p className="px-4 py-10 text-center text-sm text-muted-foreground">{stamps === null ? "Stamp data is unavailable." : "No account-owned stamps are registered."}</p>}
      </Section>
    </div>

    <div className="grid gap-px border border-border bg-border md:grid-cols-3">
      <Shortcut href="/playground" icon={<FlaskConicalIcon />} title="Inference playground" description="Compare models, streaming speed and routing." />
      <Shortcut href="/access/api-keys" icon={<KeyRoundIcon />} title="API credentials" description="Scoped keys for apps and service principals." />
      <Shortcut href="/usage" icon={<ActivityIcon />} title="Usage" description="Requests and tokens reported by each stamp." />
    </div>
  </PageContainer>
}
