import Link from "next/link"
import { BoxIcon, PlusIcon, SearchIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { LiveRefresh } from "@/components/console/live-refresh"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { getDeploymentStatuses, listDeployments, listPlacements, listStamps } from "@/lib/fabric/data"
import { deploymentState } from "@/lib/fabric/resource-state"
import type { Deployment, Stamp } from "@/lib/fabric/types"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatRelative } from "@/lib/format"

export default async function DeploymentsPage({ searchParams }: PageProps<"/deployments">) {
  const context = await getConsoleContext()
  const query = String((await searchParams).q ?? "").toLowerCase()
  let all: Deployment[] = []
  let error: string | null = null
  try { all = await listDeployments() } catch { error = "Deployment data is unavailable." }
  let stamps: Stamp[] | undefined
  if (hasScope(context, "stamps:read")) {
    try { stamps = await listStamps() } catch { error = "Stamp heartbeats could not be checked." }
  }
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

  return <PageContainer><PageHeader title="Deployments" description="Declare model-serving intent, place workloads, and track rollout convergence." actions={<Button render={<Link href="/deployments/new" />} disabled={!canWrite}><PlusIcon /> Create deployment</Button>} />
    <LiveRefresh checkedAt={checkedAt} error={error} />
    {error && !all.length ? <Alert variant="destructive"><BoxIcon /><AlertTitle>Unable to load deployments</AlertTitle><AlertDescription>Refresh to try again. No empty-fleet result has been confirmed.</AlertDescription></Alert> : null}
    <Card><CardContent className="gap-4"><form className="flex max-w-xl gap-2"><div className="relative flex-1"><SearchIcon className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input name="q" defaultValue={query} className="pl-9" placeholder="Search deployments" /></div><Button type="submit" variant="outline">Search</Button></form></CardContent>
      {deployments.length ? <div className="border-t"><Table><TableHeader><TableRow><TableHead className="pl-6">Deployment</TableHead><TableHead>Model</TableHead><TableHead>Release</TableHead><TableHead>Observed status</TableHead><TableHead>Resources</TableHead><TableHead className="pr-6 text-right">Updated</TableHead></TableRow></TableHeader><TableBody>{deployments.map((item) => { const state = states.get(item.id)!; return <TableRow key={item.id}><TableCell className="pl-6 font-medium"><Link href={`/deployments/${item.id}`} className="hover:underline">{item.name}</Link><div className="text-xs font-normal text-muted-foreground">Generation {item.generation}</div></TableCell><TableCell>{item.model_alias}</TableCell><TableCell className="font-mono text-xs">{item.desired_spec.runtime.release}</TableCell><TableCell><StatusBadge status={state.status} /><div className="mt-1 max-w-xs text-xs text-muted-foreground">{state.detail}</div></TableCell><TableCell>{state.readyReplicas ?? "—"}/{state.desiredReplicas} ready · {item.desired_spec.resources.gpu_count}× {item.desired_spec.resources.gpu_class.toUpperCase()} per replica</TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatRelative(item.updated_at)}</TableCell></TableRow>})}</TableBody></Table></div> : !error ? <Empty className="min-h-80 border-0 border-t"><EmptyHeader><EmptyMedia variant="icon"><BoxIcon /></EmptyMedia><EmptyTitle>{query ? "No matching deployments" : "Create your first deployment"}</EmptyTitle><EmptyDescription>{query ? "Try a different name or model alias." : "Define a model runtime and let Fabric place it on compatible capacity."}</EmptyDescription></EmptyHeader>{!query && canWrite ? <EmptyContent><Button render={<Link href="/deployments/new" />}><PlusIcon /> Create deployment</Button></EmptyContent> : null}</Empty> : null}
    </Card></PageContainer>
}
