import Link from "next/link"
import { BoxIcon, SearchIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { CreateDeploymentDrawer } from "@/components/console/deployment-form"
import { LiveRefresh } from "@/components/console/live-refresh"
import { Section, SectionEmpty } from "@/components/console/section"
import { StatusBadge } from "@/components/console/status-badge"
import { TruncatedText } from "@/components/console/truncated-text"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { getDeploymentStatuses, listDeployments, listPlacements, listStamps } from "@/lib/fabric/data"
import { deploymentState } from "@/lib/fabric/resource-state"
import { deploymentStampChoices } from "@/lib/fabric/model-catalog"
import type { Deployment, Stamp } from "@/lib/fabric/types"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatRelative } from "@/lib/format"

export default async function DeploymentsPage({ searchParams }: PageProps<"/deployments">) {
  const context = await getConsoleContext()
  const query = String((await searchParams).q ?? "").toLowerCase()
  let all: Deployment[] = []
  let error: string | null = null
  let stamps: Stamp[] | undefined
  const [deploymentResult, stampResult] = await Promise.allSettled([
    listDeployments(),
    hasScope(context, "stamps:read") ? listStamps() : Promise.resolve(undefined),
  ])
  if (deploymentResult.status === "fulfilled") all = deploymentResult.value
  else error = "Deployment data is unavailable."
  if (stampResult.status === "fulfilled") stamps = stampResult.value
  else error = error ?? "Stamp heartbeats could not be checked."
  const deployments = query ? all.filter((item) => item.name.toLowerCase().includes(query) || item.model_alias.toLowerCase().includes(query)) : all
  const states = new Map<string, ReturnType<typeof deploymentState>>()
  const observationFailures: string[] = []
  for (let index = 0; index < deployments.length; index += 4) {
    await Promise.all(deployments.slice(index, index + 4).map(async (item) => {
      try {
        const [statuses, placements] = await Promise.all([getDeploymentStatuses(item.id), listPlacements(item.id)])
        states.set(item.id, deploymentState(item, statuses, placements, stamps))
      } catch { states.set(item.id, deploymentState(item)); observationFailures.push(item.id) }
    }))
  }
  if (observationFailures.length) error = "Some workload observations could not be checked."
  const checkedAt = new Date().toISOString()
  const canWrite = hasScope(context, "deployments:write")
  const stampOptions = deploymentStampChoices(stamps ?? [])
  const createDrawer = (disabled: boolean) => <CreateDeploymentDrawer stamps={stampOptions} infrastructureUnavailable={stampResult.status === "rejected"} disabled={disabled} />

  return <PageContainer><PageHeader title="Deployments" description="Declare model-serving intent, place workloads, and track rollout convergence." actions={createDrawer(!canWrite)} />
    <LiveRefresh checkedAt={checkedAt} error={error} />
    {error && !all.length ? <Alert variant="destructive"><BoxIcon /><AlertTitle>Unable to load deployments</AlertTitle><AlertDescription>Refresh to try again. No empty-fleet result has been confirmed.</AlertDescription></Alert> : null}
    <Section title="All deployments" description={query ? `${deployments.length} of ${all.length} matching “${query}”` : undefined} action={<form className="flex w-full max-w-sm gap-2 sm:w-72"><div className="relative flex-1"><SearchIcon className="absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input name="q" defaultValue={query} className="pl-8" placeholder="Search name or model" aria-label="Search deployments" /></div><Button type="submit" variant="outline">Search</Button></form>}>
      {deployments.length ? <Table className="min-w-[960px]"><TableHeader><TableRow><TableHead>Deployment</TableHead><TableHead>Model</TableHead><TableHead>Release</TableHead><TableHead className="min-w-56">Observed status</TableHead><TableHead className="text-right">Resources</TableHead><TableHead className="text-right">Updated</TableHead></TableRow></TableHeader><TableBody>{deployments.map((item) => { const state = states.get(item.id)!; return <TableRow key={item.id}><TableCell className="h-14"><Link href={`/deployments/${item.id}`} className="block max-w-56 font-medium hover:underline"><TruncatedText text={item.name} /></Link><div className="text-xs text-muted-foreground tabular-nums">Generation {item.generation}</div></TableCell><TableCell><TruncatedText text={item.model_alias} className="max-w-48 font-mono text-xs" /></TableCell><TableCell><TruncatedText text={item.desired_spec.runtime.release} className="max-w-56 font-mono text-xs text-muted-foreground" /></TableCell><TableCell className="whitespace-normal"><StatusBadge status={state.status} /><div className="mt-0.5 max-w-xs text-xs text-muted-foreground">{state.detail}</div></TableCell><TableCell className="text-right"><div className="tabular-nums">{state.readyReplicas ?? "—"}/{state.desiredReplicas} ready</div><div className="text-xs text-muted-foreground tabular-nums">{item.desired_spec.resources.gpu_count}× {item.desired_spec.resources.gpu_class.toUpperCase()} per replica</div></TableCell><TableCell className="text-right text-muted-foreground tabular-nums">{formatRelative(item.updated_at)}</TableCell></TableRow>})}</TableBody></Table> : !error ? <Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><BoxIcon /></EmptyMedia><EmptyTitle>{query ? "No matching deployments" : "Create your first deployment"}</EmptyTitle><EmptyDescription>{query ? "Try a different name or model alias." : "Define a model runtime and let Fabric place it on compatible capacity."}</EmptyDescription></EmptyHeader>{!query && canWrite ? <EmptyContent>{createDrawer(false)}</EmptyContent> : null}</Empty> : <SectionEmpty>Deployment data is unavailable.</SectionEmpty>}
    </Section></PageContainer>
}
