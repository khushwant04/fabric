import { ArrowRightIcon, FingerprintIcon, ShieldAlertIcon } from "lucide-react"

import { IdentityProviderDrawer, RemoveIdentityProviderDialog } from "@/components/console/identity-provider"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { getOidcProvider } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import type { OidcProvider } from "@/lib/fabric/types"
import { formatDate, formatRelative } from "@/lib/format"

function Row({ label, children, mono = false }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return <div className="grid gap-1 px-4 py-3 sm:grid-cols-[180px_minmax(0,1fr)]"><dt className="text-muted-foreground">{label}</dt><dd className={mono ? "min-w-0 font-mono text-xs leading-5 break-all" : "min-w-0 font-medium"}>{children}</dd></div>
}

export default async function IdentityProviderPage() {
  const context = await getConsoleContext()
  const canRead = hasScope(context, "members:read")
  const canWrite = hasScope(context, "members:write")
  let provider: OidcProvider | null = null
  let failed = false
  if (canRead) {
    try {
      provider = await getOidcProvider()
    } catch {
      failed = true
    }
  }

  return <PageContainer>
    <PageHeader title="Identity provider" description="Bring your own OpenID Connect issuer so people sign in to this account with your directory." actions={canRead && !failed ? <IdentityProviderDrawer provider={provider} disabled={!canWrite} /> : null} />
    {!canRead ? <Alert><ShieldAlertIcon /><AlertTitle>Not available with your role</AlertTitle><AlertDescription>Viewing the identity provider needs the members:read scope.</AlertDescription></Alert>
      : failed ? <Alert variant="destructive"><ShieldAlertIcon /><AlertTitle>Identity provider unavailable</AlertTitle><AlertDescription>The control plane could not be reached. Refresh to try again.</AlertDescription></Alert>
      : provider ? <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card>
          <CardHeader><CardTitle className="flex flex-wrap items-center gap-2">Registered provider <StatusBadge status={provider.status} /></CardTitle><CardDescription>Tokens from this issuer are verified with its signing keys and mapped to people in this account.</CardDescription></CardHeader>
          <CardContent><dl className="divide-y rounded-lg border text-sm">
            <Row label="Issuer" mono>{provider.issuer}</Row>
            <Row label="Audience" mono>{provider.audience}</Row>
            <Row label="Signing keys" mono>{provider.jwks_uri}</Row>
            <Row label="Subject claim"><code className="font-mono text-xs">{provider.subject_claim}</code> <span className="font-normal text-muted-foreground">identifies the person</span></Row>
            <Row label="Email claim"><code className="font-mono text-xs">{provider.email_claim}</code> <span className="font-normal text-muted-foreground">recorded as their email</span></Row>
            <Row label="First sign-in">{provider.auto_provision_role ? <>Joins as <Badge variant="secondary" className="ml-1 capitalize">{provider.auto_provision_role}</Badge></> : <span className="font-normal">Admitted individually under Members</span>}</Row>
            <Row label="Registered">{formatDate(provider.created_at)}</Row>
            <Row label="Last changed">{formatRelative(provider.updated_at)}</Row>
          </dl></CardContent>
          {canWrite ? <CardFooter className="justify-end gap-2"><RemoveIdentityProviderDialog issuer={provider.issuer} /></CardFooter> : null}
        </Card>
        <Card className="h-fit">
          <CardHeader><CardTitle>How sign-in works</CardTitle></CardHeader>
          <CardContent><ol className="space-y-3 text-sm text-muted-foreground">
            <li className="flex gap-2"><ArrowRightIcon className="mt-0.5 size-3.5 shrink-0" />A person presents a token from this issuer for the audience above.</li>
            <li className="flex gap-2"><ArrowRightIcon className="mt-0.5 size-3.5 shrink-0" />Fabric checks the signature, issuer, audience and expiry.</li>
            <li className="flex gap-2"><ArrowRightIcon className="mt-0.5 size-3.5 shrink-0" />The subject claim selects the person; their membership decides the scopes in the Fabric token issued for this account.</li>
          </ol><p className="mt-4 text-xs text-muted-foreground">Changing the provider is recorded in the audit log. Only owners and admins can change it.</p></CardContent>
        </Card>
      </div>
      : <Card><Empty className="min-h-80 border-0"><EmptyHeader><EmptyMedia variant="icon"><FingerprintIcon /></EmptyMedia><EmptyTitle>No identity provider registered</EmptyTitle><EmptyDescription>People sign in through the console’s default identity provider. Register your own OpenID Connect issuer to use your directory instead.</EmptyDescription></EmptyHeader>{canWrite ? <EmptyContent><IdentityProviderDrawer provider={null} variant="outline" /></EmptyContent> : null}</Empty></Card>}
  </PageContainer>
}
