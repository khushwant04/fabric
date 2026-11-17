import Link from "next/link"
import { notFound } from "next/navigation"
import { ArrowLeftIcon, GaugeIcon, LockIcon, ServerCrashIcon } from "lucide-react"

import { DeleteRunDialog } from "@/components/agents/delete-run-dialog"
import { BudgetMeter, RunStatusBadge } from "@/components/agents/run-status"
import { formatLatency, RunStepTimeline } from "@/components/agents/run-timeline"
import { LiveRefresh } from "@/components/console/live-refresh"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Metric, MetricGrid, Section, SectionBody } from "@/components/console/section"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { getAgentRun } from "@/lib/fabric/agent-data"
import { AgentRunsError, isRunId, type RunTrace } from "@/lib/fabric/agent-runs"
import { formatDate, formatNumber, formatRelative } from "@/lib/format"

export default async function AgentRunPage({ params }: PageProps<"/agents/runs/[runId]">) {
  const raw = (await params).runId
  let runId = raw
  try { runId = decodeURIComponent(raw) } catch { notFound() }
  if (!isRunId(runId)) notFound()
  let run: RunTrace | null = null
  let error: AgentRunsError | null = null
  try {
    run = await getAgentRun(runId)
  } catch (caught) {
    if (!(caught instanceof AgentRunsError)) throw caught
    error = caught
  }
  const checkedAt = new Date().toISOString()
  const back = <Button nativeButton={false} variant="outline" render={<Link href="/agents/runs" />}><ArrowLeftIcon /> All runs</Button>

  if (!run) {
    return <PageContainer>
      <PageHeader title="Run" description={runId} actions={back} />
      <Alert variant={error?.kind === "unsupported" ? "default" : "destructive"}><ServerCrashIcon />
        <AlertTitle>{error ? error.kind === "unsupported" ? "Agent runs are not available on this gateway" : "Unable to load this run" : "Run not found"}</AlertTitle>
        <AlertDescription>{error ? error.message : "The gateway has no run with this id for your account. Runs expire after an hour without activity and are forgotten when the gateway restarts."}</AlertDescription>
      </Alert>
    </PageContainer>
  }

  const latency = run.steps.reduce((total, step) => total + (step.latencyMs ?? 0), 0)
  const exhaustedBy = run.status === "budget_exhausted" ? run.budget.tokens && run.totals.tokens >= run.budget.tokens ? "token" : "step" : null

  return <PageContainer>
    <PageHeader title="Run trace" description={`Started ${formatDate(run.createdAt)} · last activity ${formatRelative(run.updatedAt)}`} actions={<>{back}<DeleteRunDialog runId={run.runId} /></>}>
      <div className="flex flex-wrap items-center gap-3 pt-1"><code className="rounded-md border border-border bg-muted/50 px-2 py-0.5 font-mono text-[13px]">{run.runId}</code><RunStatusBadge status={run.status} /></div>
    </PageHeader>
    <LiveRefresh checkedAt={checkedAt} intervalMs={5000} />
    {exhaustedBy ? <Alert><GaugeIcon /><AlertTitle>Budget exhausted</AlertTitle><AlertDescription>The run&apos;s {exhaustedBy} budget is spent. The gateway refuses further steps with <code>429 run_budget_exhausted</code> without calling the model{run.totals.budgetRefusals ? ` and has refused ${run.totals.budgetRefusals} so far` : ""}.</AlertDescription></Alert> : null}
    <Section title="Budget" description="Set by the first request of the run. A started step always finishes, so the last one can overshoot the token budget.">
      <SectionBody className="grid gap-6 md:grid-cols-2">
        <BudgetMeter used={run.totals.tokens} budget={run.budget.tokens} unit="tokens" size="default" label="Tokens" />
        <BudgetMeter used={run.totals.steps} budget={run.budget.steps} unit="steps" size="default" label="Steps" />
      </SectionBody>
    </Section>
    <MetricGrid className="grid-cols-2 lg:grid-cols-4">
      <Metric label="Input tokens" value={formatNumber(run.totals.inputTokens)} detail="Across all steps" />
      <Metric label="Output tokens" value={formatNumber(run.totals.outputTokens)} detail="Across all steps" />
      <Metric label="Model latency" value={formatLatency(latency)} detail={`Across ${run.steps.length} traced step${run.steps.length === 1 ? "" : "s"}`} />
      <Metric label="Guard repairs" value={formatNumber(run.totals.toolGuardRepairs)} detail={`${run.totals.toolGuardFailures} failed · ${run.totals.budgetRefusals} budget refusals`} />
    </MetricGrid>
    <Section title="Steps" description={<span className="flex items-center gap-1.5"><LockIcon className="size-3.5 shrink-0" />Counts, tool names and outcomes only. The gateway never stores prompts, completions or tool arguments.</span>}>
      <SectionBody>{run.steps.length ? <RunStepTimeline steps={run.steps} dropped={run.stepsDropped} /> : <p className="text-sm text-muted-foreground">No steps yet. The run exists because a request carried its id{run.totals.budgetRefusals ? " and was refused" : ""}.</p>}</SectionBody>
    </Section>
  </PageContainer>
}
