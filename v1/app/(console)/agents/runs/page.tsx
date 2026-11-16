import Link from "next/link"
import { BotIcon, CheckIcon, FlaskConicalIcon, ServerCrashIcon, WorkflowIcon } from "lucide-react"

import { BudgetMeter, RunStatusBadge } from "@/components/agents/run-status"
import { StartRunSnippet } from "@/components/agents/start-run-snippet"
import { LiveRefresh } from "@/components/console/live-refresh"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { TruncatedText } from "@/components/console/truncated-text"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listAgentRuns } from "@/lib/fabric/agent-data"
import { filterRuns, parseStatusFilter, type RunStatusFilter } from "@/lib/fabric/agent-runs"
import { formatNumber, formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"

const filters: Array<{ value: RunStatusFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "budget_exhausted", label: "Budget exhausted" },
]

export default async function AgentRunsPage({ searchParams }: PageProps<"/agents/runs">) {
  const query = await searchParams
  const status = parseStatusFilter(query.status)
  const { runs: all, error, unsupported, partial } = await listAgentRuns()
  const runs = filterRuns(all, status)
  const checkedAt = new Date().toISOString()
  const counts = { all: all.length, active: all.filter((run) => run.status === "active").length, budget_exhausted: all.filter((run) => run.status === "budget_exhausted").length }

  return <PageContainer>
    <PageHeader title="Runs" description="Budgets, step traces and tool-call guard repairs for agent loops on your inference gateways. Traces hold counts and outcomes only, never prompts or completions." actions={<Button nativeButton={false} render={<Link href="/agents/playground" />}><FlaskConicalIcon /> Agent playground</Button>} />
    <LiveRefresh checkedAt={checkedAt} error={partial ? error : null} />
    {query.deleted === "1" ? <Alert><CheckIcon /><AlertTitle>Run deleted</AlertTitle><AlertDescription>The gateway no longer holds that trace or its budget.</AlertDescription></Alert> : null}
    {unsupported ? <Alert><ServerCrashIcon /><AlertTitle>Agent runs are not available on this gateway</AlertTitle><AlertDescription>{error} Model requests keep working; only run budgets and traces need the newer gateway.</AlertDescription></Alert>
      : error && !partial ? <Alert variant="destructive"><ServerCrashIcon /><AlertTitle>Unable to load runs</AlertTitle><AlertDescription>{error} No empty result has been confirmed.</AlertDescription></Alert> : null}
    {!error || partial ? <Card>
      <CardContent className="flex flex-wrap items-center justify-between gap-3">
        <nav aria-label="Filter runs by status" className="flex flex-wrap gap-1 rounded-xl bg-muted/50 p-1">
          {filters.map((filter) => <Link key={filter.value} href={filter.value === "all" ? "/agents/runs" : `/agents/runs?status=${filter.value}`} aria-current={status === filter.value ? "page" : undefined}
            className={cn("rounded-lg px-3 py-1.5 text-[13px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring", status === filter.value && "bg-background font-medium text-foreground shadow-sm")}>
            {filter.label}<span className="ml-1.5 tabular-nums text-muted-foreground">{counts[filter.value]}</span>
          </Link>)}
        </nav>
        <p className="text-xs text-muted-foreground">Runs live in gateway memory and expire after an hour without activity.</p>
      </CardContent>
      {runs.length ? <div className="border-t"><Table className="min-w-[900px]">
        <TableHeader><TableRow>
          <TableHead className="pl-6">Run</TableHead><TableHead>Status</TableHead><TableHead className="text-right">Steps</TableHead>
          <TableHead className="w-56">Tokens</TableHead><TableHead className="text-right">Refusals</TableHead><TableHead className="text-right">Guard repairs</TableHead>
          <TableHead className="pr-6 text-right">Last activity</TableHead>
        </TableRow></TableHeader>
        <TableBody>{runs.map((run) => <TableRow key={run.runId}>
          <TableCell className="pl-6"><Link href={`/agents/runs/${encodeURIComponent(run.runId)}`} className="block max-w-64 font-mono text-[13px] font-medium hover:underline"><TruncatedText text={run.runId} /></Link><div className="text-xs text-muted-foreground">Started {formatRelative(run.createdAt)}</div></TableCell>
          <TableCell><RunStatusBadge status={run.status} /></TableCell>
          <TableCell className="text-right tabular-nums">{run.totals.steps}{run.budget.steps ? <span className="text-muted-foreground"> / {run.budget.steps}</span> : null}</TableCell>
          <TableCell><BudgetMeter used={run.totals.tokens} budget={run.budget.tokens} unit="tokens" /></TableCell>
          <TableCell className={cn("text-right tabular-nums", run.totals.budgetRefusals ? "text-status-warning" : "text-muted-foreground")}>{formatNumber(run.totals.budgetRefusals)}</TableCell>
          <TableCell className="text-right tabular-nums">{run.totals.toolGuardRepairs || run.totals.toolGuardFailures ? <>{formatNumber(run.totals.toolGuardRepairs)}{run.totals.toolGuardFailures ? <span className="text-destructive"> · {run.totals.toolGuardFailures} failed</span> : null}</> : <span className="text-muted-foreground">0</span>}</TableCell>
          <TableCell className="pr-6 text-right text-muted-foreground">{formatRelative(run.updatedAt ?? run.createdAt)}</TableCell>
        </TableRow>)}</TableBody>
      </Table></div> : all.length ? <Empty className="min-h-56 border-0 border-t"><EmptyHeader><EmptyMedia variant="icon"><WorkflowIcon /></EmptyMedia><EmptyTitle>No runs with this status</EmptyTitle><EmptyDescription>Choose another filter to see the account&apos;s other runs.</EmptyDescription></EmptyHeader></Empty>
        : <Empty className="border-0 border-t py-12"><EmptyHeader><EmptyMedia variant="icon"><BotIcon /></EmptyMedia><EmptyTitle>No agent runs yet</EmptyTitle><EmptyDescription className="max-w-xl">Tag each model call of a task with an <code className="font-mono text-foreground">X-Fabric-Run-Id</code> header, optionally with a token and step budget. The gateway enforces the budget and records a trace here. Server-side agent runs from the playground appear automatically.</EmptyDescription></EmptyHeader>
          <EmptyContent className="max-w-4xl"><StartRunSnippet /><Button nativeButton={false} variant="outline" render={<Link href="/agents/playground" />}><FlaskConicalIcon /> Start a run in the agent playground</Button></EmptyContent></Empty>}
    </Card> : null}
  </PageContainer>
}
