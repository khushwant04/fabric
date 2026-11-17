import Link from "next/link"
import { RouteIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Section, SectionEmpty } from "@/components/console/section"
import { TruncatedText } from "@/components/console/truncated-text"
import { Status } from "@/components/hexel/status"
import { CreatePolicyDialog } from "@/components/routing/create-policy-dialog"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listRoutingPolicies, routingDeploymentOptions } from "@/lib/fabric/routing-data"
import type { DeploymentOption, RoutingPolicySummary } from "@/lib/fabric/routing-policy"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatRelative } from "@/lib/format"

export default async function RoutingPage() {
  const context = await getConsoleContext()
  const canWrite = hasScope(context, "deployments:write")
  const [policyResult, deploymentResult] = await Promise.allSettled([listRoutingPolicies(), routingDeploymentOptions()])
  const policies: RoutingPolicySummary[] = policyResult.status === "fulfilled" ? policyResult.value : []
  const deployments: DeploymentOption[] = deploymentResult.status === "fulfilled" ? deploymentResult.value : []
  const names = new Map(deployments.map((item) => [item.id, item.name]))
  const create = <CreatePolicyDialog deployments={deployments} disabled={!canWrite} />

  return <PageContainer>
    <PageHeader title="Routing" description="Give clients one model name and decide, rule by rule, which deployments answer it: canary splits, fallbacks and stamp preference." actions={create} />
    {policyResult.status === "rejected" ? <Alert variant="destructive"><RouteIcon /><AlertTitle>Unable to load routing policies</AlertTitle><AlertDescription>Refresh to try again.</AlertDescription></Alert> : null}
    <Section title="Policies" description="Published versions are delivered to stamps and the edge with their configuration and enforced there, even while the control plane is unreachable.">
      {policies.length ? <Table className="min-w-[860px]">
        <TableHeader><TableRow>
          <TableHead>Model name</TableHead>
          <TableHead>Live version</TableHead>
          <TableHead className="text-right">Rules</TableHead>
          <TableHead>Deployments</TableHead>
          <TableHead className="text-right">Updated</TableHead>
        </TableRow></TableHeader>
        <TableBody>{policies.map((policy) => <TableRow key={policy.id}>
          <TableCell className="h-14">
            <Link href={`/routing/${policy.id}`} className="block max-w-64 font-mono text-[13px] font-medium hover:underline"><TruncatedText text={policy.alias} /></Link>
            {policy.description ? <div className="max-w-64 text-xs text-muted-foreground"><TruncatedText text={policy.description} /></div> : null}
          </TableCell>
          <TableCell className="whitespace-normal">
            {policy.live_version ? <Status tone={policy.has_unpublished_changes ? "warning" : "good"}>v{policy.live_version}{policy.has_unpublished_changes ? " · draft changes" : ""}</Status> : <Status tone="neutral">Draft only</Status>}
            {policy.published_at ? <div className="mt-0.5 text-xs text-muted-foreground">Published {formatRelative(policy.published_at)}</div> : null}
          </TableCell>
          <TableCell className="text-right tabular-nums">{policy.rule_count}</TableCell>
          <TableCell className="whitespace-normal"><div className="flex max-w-sm flex-wrap gap-1">{policy.deployment_ids.slice(0, 4).map((id) => <span key={id} className="rounded-md bg-muted px-1.5 py-0.5 text-xs">{names.get(id) ?? "Unknown"}</span>)}{policy.deployment_ids.length > 4 ? <span className="px-1 text-xs text-muted-foreground">+{policy.deployment_ids.length - 4}</span> : null}</div></TableCell>
          <TableCell className="text-right text-muted-foreground tabular-nums">{formatRelative(policy.updated_at)}</TableCell>
        </TableRow>)}</TableBody>
      </Table> : policyResult.status === "fulfilled" ? <Empty className="min-h-72">
        <EmptyHeader>
          <EmptyMedia variant="icon"><RouteIcon /></EmptyMedia>
          <EmptyTitle>Route one model name to many deployments</EmptyTitle>
          <EmptyDescription>{deployments.length ? "Create a policy, then add rules for tool calls, long prompts or specific API keys, and canary a new deployment with a weighted split." : "Create a deployment first; policies send traffic to your deployments."}</EmptyDescription>
        </EmptyHeader>
        {canWrite && deployments.length ? <EmptyContent>{create}</EmptyContent> : null}
      </Empty> : <SectionEmpty>Routing policies are unavailable.</SectionEmpty>}
    </Section>
  </PageContainer>
}
