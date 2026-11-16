import { ArrowRightIcon, FingerprintIcon, ShieldAlertIcon } from "lucide-react"

import { IdentityProviderDrawer, RemoveIdentityProviderDialog } from "@/components/console/identity-provider"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Field, FieldList, Section, SectionBody, SectionFooter } from "@/components/console/section"
import { StatusBadge } from "@/components/console/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { getOidcProvider } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import type { OidcProvider } from "@/lib/fabric/types"
import { formatDate, formatRelative } from "@/lib/format"

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
      : provider ? <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Section title="Registered provider" description="Tokens from this issuer are verified with its signing keys and mapped to people in this account." action={<StatusBadge status={provider.status} />}>
          <FieldList>
            <Field label="Issuer" mono>{provider.issuer}</Field>
            <Field label="Audience" mono>{provider.audience}</Field>
            <Field label="Signing keys" mono>{provider.jwks_uri}</Field>
            <Field label="Subject claim"><code className="font-mono text-xs">{provider.subject_claim}</code> <span className="font-normal text-muted-foreground">identifies the person</span></Field>
            <Field label="Email claim"><code className="font-mono text-xs">{provider.email_claim}</code> <span className="font-normal text-muted-foreground">recorded as their email</span></Field>
            <Field label="First sign-in">{provider.auto_provision_role ? <>Joins as <Badge variant="secondary" className="ml-1 capitalize">{provider.auto_provision_role}</Badge></> : <span className="font-normal">Admitted individually under Members</span>}</Field>
            <Field label="Registered">{formatDate(provider.created_at)}</Field>
            <Field label="Last changed">{formatRelative(provider.updated_at)}</Field>
          </FieldList>
          {canWrite ? <SectionFooter className="justify-end"><RemoveIdentityProviderDialog issuer={provider.issuer} /></SectionFooter> : null}
        </Section>
        <Section title="How sign-in works">
          <SectionBody><ol className="space-y-3 text-muted-foreground">
            <li className="flex gap-2"><ArrowRightIcon className="mt-0.5 size-3.5 shrink-0" />A person presents a token from this issuer for the audience above.</li>
            <li className="flex gap-2"><ArrowRightIcon className="mt-0.5 size-3.5 shrink-0" />Fabric checks the signature, issuer, audience and expiry.</li>
            <li className="flex gap-2"><ArrowRightIcon className="mt-0.5 size-3.5 shrink-0" />The subject claim selects the person; their membership decides the scopes in the Fabric token issued for this account.</li>
          </ol><p className="border-t border-border pt-3 text-xs text-muted-foreground">Changing the provider is recorded in the audit log. Only owners and admins can change it.</p></SectionBody>
        </Section>
      </div>
      : <Section title="Identity provider"><Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><FingerprintIcon /></EmptyMedia><EmptyTitle>No identity provider registered</EmptyTitle><EmptyDescription>People sign in through the console’s default identity provider. Register your own OpenID Connect issuer to use your directory instead.</EmptyDescription></EmptyHeader>{canWrite ? <EmptyContent><IdentityProviderDrawer provider={null} variant="outline" /></EmptyContent> : null}</Empty></Section>}
  </PageContainer>
}
