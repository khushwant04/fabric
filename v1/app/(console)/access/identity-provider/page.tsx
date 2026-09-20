import { NetworkIcon, ShieldCheckIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { OidcProviderForm } from "@/components/console/resource-dialogs"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { getOidcProvider } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate } from "@/lib/format"

export default async function IdentityProviderPage() {
  const [context, provider] = await Promise.all([getConsoleContext(), getOidcProvider()])
  const canWrite = hasScope(context, "members:write") && !context.demo
  return <PageContainer><PageHeader title="Identity provider" description="Trust one account-owned OIDC directory for workforce authentication." />
    <Alert><ShieldCheckIcon /><AlertTitle>Account trust boundary</AlertTitle><AlertDescription>Issuer matching is exact. Removing a provider blocks future exchanges but does not revoke tokens already issued.</AlertDescription></Alert>
    <div className="grid gap-5 xl:grid-cols-[1fr_300px]"><Card><CardHeader><CardTitle>OIDC configuration</CardTitle><CardDescription>Use discovery where possible so the provider remains authoritative for signing keys.</CardDescription></CardHeader><CardContent><OidcProviderForm provider={provider} disabled={!canWrite} /></CardContent></Card>
      <Card className="h-fit"><CardHeader><CardTitle className="flex items-center gap-2"><NetworkIcon className="size-4" /> Connection</CardTitle></CardHeader><CardContent>{provider ? <dl className="space-y-4 text-sm"><div><dt className="text-xs text-muted-foreground">Status</dt><dd className="mt-1"><StatusBadge status={provider.status} /></dd></div><div><dt className="text-xs text-muted-foreground">Issuer</dt><dd className="mt-1 break-all">{provider.issuer}</dd></div><div><dt className="text-xs text-muted-foreground">Updated</dt><dd className="mt-1">{formatDate(provider.updated_at)}</dd></div></dl> : <p className="text-sm leading-6 text-muted-foreground">No external identity provider is configured.</p>}</CardContent></Card></div>
  </PageContainer>
}
