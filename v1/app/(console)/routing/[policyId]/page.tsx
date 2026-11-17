import { notFound } from "next/navigation"

import { PageContainer } from "@/components/console/page-header"
import { PolicyEditor } from "@/components/routing/policy-editor"
import { listApiKeys, listServicePrincipals, listStamps } from "@/lib/fabric/data"
import { getRoutingPolicy, listRoutingPolicyVersions, routingDeploymentOptions } from "@/lib/fabric/routing-data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"

export default async function RoutingPolicyPage({ params, searchParams }: PageProps<"/routing/[policyId]">) {
  const { policyId } = await params
  const tab = String((await searchParams).tab ?? "test")
  const context = await getConsoleContext()
  const policy = await getRoutingPolicy(policyId)
  if (!policy) notFound()
  const optional = <T,>(allowed: boolean, load: () => Promise<T[]>) => (allowed ? load().catch(() => [] as T[]) : Promise.resolve([] as T[]))
  const [versions, deployments, keys, principals, stamps] = await Promise.all([
    listRoutingPolicyVersions(policyId).catch(() => []),
    routingDeploymentOptions().catch(() => []),
    optional(hasScope(context, "api-keys:read"), listApiKeys),
    optional(hasScope(context, "api-keys:read"), listServicePrincipals),
    optional(hasScope(context, "stamps:read"), listStamps),
  ])
  return <PageContainer>
    <PolicyEditor
      key={policy.id}
      initial={policy}
      versions={versions}
      deployments={deployments}
      apiKeys={keys.filter((key) => !key.revoked_at).map((key) => ({ id: key.id, name: `${key.name} (${key.key_prefix})` }))}
      principals={principals.map((item) => ({ id: item.id, name: item.name }))}
      stamps={stamps.filter((stamp) => !stamp.revoked_at).map((stamp) => ({ id: stamp.id, name: stamp.name }))}
      canWrite={hasScope(context, "deployments:write")}
      initialTab={["test", "versions", "checks"].includes(tab) ? tab : "test"}
    />
  </PageContainer>
}
