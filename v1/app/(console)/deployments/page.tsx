import Link from "next/link"
import { BoxIcon, PlusIcon, SearchIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge } from "@/components/console/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listDeployments } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatRelative } from "@/lib/format"

export default async function DeploymentsPage({ searchParams }: PageProps<"/deployments">) {
  const context = await getConsoleContext()
  const query = String((await searchParams).q ?? "").toLowerCase()
  const all = await listDeployments()
  const deployments = query ? all.filter((item) => item.name.toLowerCase().includes(query) || item.model_alias.toLowerCase().includes(query)) : all
  const canWrite = hasScope(context, "deployments:write") && !context.demo

  return <PageContainer><PageHeader title="Deployments" description="Declare model-serving intent, place workloads, and track rollout convergence." actions={<Button render={<Link href="/deployments/new" />} disabled={!canWrite}><PlusIcon /> Create deployment</Button>} />
    <Card><CardContent className="gap-4"><form className="flex max-w-xl gap-2"><div className="relative flex-1"><SearchIcon className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input name="q" defaultValue={query} className="pl-9" placeholder="Search deployments" /></div><Button type="submit" variant="outline">Search</Button></form></CardContent>
      {deployments.length ? <div className="border-t"><Table><TableHeader><TableRow><TableHead className="pl-6">Deployment</TableHead><TableHead>Model</TableHead><TableHead>Release</TableHead><TableHead>Status</TableHead><TableHead>Resources</TableHead><TableHead className="pr-6 text-right">Updated</TableHead></TableRow></TableHeader><TableBody>{deployments.map((item) => <TableRow key={item.id}><TableCell className="pl-6 font-medium"><Link href={`/deployments/${item.id}`} className="hover:underline">{item.name}</Link><div className="text-xs font-normal text-muted-foreground">Generation {item.generation}</div></TableCell><TableCell>{item.model_alias}</TableCell><TableCell className="font-mono text-xs">{item.desired_spec.runtime.release}</TableCell><TableCell><StatusBadge status={item.status} /></TableCell><TableCell>{item.desired_spec.replicas} replica{item.desired_spec.replicas === 1 ? "" : "s"} · {item.desired_spec.resources.gpu_count}× {item.desired_spec.resources.gpu_class.toUpperCase()}</TableCell><TableCell className="pr-6 text-right text-muted-foreground">{formatRelative(item.updated_at)}</TableCell></TableRow>)}</TableBody></Table></div> : <Empty className="min-h-80 border-0 border-t"><EmptyHeader><EmptyMedia variant="icon"><BoxIcon /></EmptyMedia><EmptyTitle>{query ? "No matching deployments" : "Create your first deployment"}</EmptyTitle><EmptyDescription>{query ? "Try a different name or model alias." : "Define a model runtime and let Fabric place it on compatible capacity."}</EmptyDescription></EmptyHeader>{!query && canWrite ? <EmptyContent><Button render={<Link href="/deployments/new" />}><PlusIcon /> Create deployment</Button></EmptyContent> : null}</Empty>}
    </Card></PageContainer>
}
