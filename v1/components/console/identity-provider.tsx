"use client"

import { useActionState, useState } from "react"
import { FingerprintIcon, PencilIcon, PlusIcon, Trash2Icon } from "lucide-react"

import { FormDrawer } from "@/components/console/form-drawer"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogMedia, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { removeIdentityProvider, saveIdentityProvider, type IdentityProviderActionState } from "@/lib/fabric/identity-provider-actions"
import { autoProvisionRoles } from "@/lib/fabric/identity-provider"
import type { OidcProvider } from "@/lib/fabric/types"

/** Register a provider, or edit the registered one. Saving replaces it (the API's PUT). */
export function IdentityProviderDrawer({ provider, disabled = false, variant }: {
  provider: OidcProvider | null
  disabled?: boolean
  variant?: "default" | "outline"
}) {
  const editing = provider !== null
  return <FormDrawer<IdentityProviderActionState>
    trigger={{ label: editing ? "Edit provider" : "Register provider", icon: editing ? PencilIcon : PlusIcon, disabled, variant }}
    title={editing ? "Edit identity provider" : "Register identity provider"}
    description="Let people sign in to this account with tokens from your own OpenID Connect issuer."
    icon={FingerprintIcon}
    action={saveIdentityProvider}
    submitLabel={editing ? "Save changes" : "Register provider"}
    pendingLabel="Saving…"
    submitIcon={FingerprintIcon}
    renderSuccess={() => <Alert><FingerprintIcon /><AlertTitle>Identity provider saved</AlertTitle><AlertDescription>Tokens from this issuer are verified for this account from now on. Tokens already issued stay valid until they expire.</AlertDescription></Alert>}
  >
    {({ markDirty }) => <FieldGroup>
      <Field><FieldLabel htmlFor="issuer">Issuer URL</FieldLabel><Input id="issuer" name="issuer" type="url" inputMode="url" placeholder="https://login.example.com" defaultValue={provider?.issuer} required maxLength={500} autoComplete="off" /><FieldDescription>Matched exactly against the <code className="font-mono text-xs">iss</code> claim. An issuer can belong to one account only.</FieldDescription></Field>
      <Field><FieldLabel htmlFor="audience">Audience</FieldLabel><Input id="audience" name="audience" placeholder="fabric-console" defaultValue={provider?.audience} required maxLength={500} autoComplete="off" /><FieldDescription>The client ID or API identifier your provider puts in the <code className="font-mono text-xs">aud</code> claim for Fabric.</FieldDescription></Field>
      <Field><FieldLabel htmlFor="jwksUri">JWKS URI (optional)</FieldLabel><Input id="jwksUri" name="jwksUri" type="url" inputMode="url" placeholder="Discovered from the issuer" maxLength={500} autoComplete="off" /><FieldDescription>{provider ? <>Leave empty to discover the signing keys from the issuer’s metadata again. Currently <span className="break-all font-mono text-xs">{provider.jwks_uri}</span>.</> : "Leave empty to discover the signing keys from the issuer’s metadata, which is preferable."}</FieldDescription></Field>
      <div className="rounded-xl border border-border p-4"><FieldSet>
        <FieldLegend variant="label" className="tracking-wide text-muted-foreground uppercase">Claim mappings</FieldLegend>
        <FieldDescription>Which token claims identify the person.</FieldDescription>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field><FieldLabel htmlFor="subjectClaim">Subject claim</FieldLabel><Input id="subjectClaim" name="subjectClaim" placeholder="sub" defaultValue={provider?.subject_claim ?? "sub"} maxLength={100} pattern="[A-Za-z0-9_:./\-]+" autoComplete="off" className="font-mono" /></Field>
          <Field><FieldLabel htmlFor="emailClaim">Email claim</FieldLabel><Input id="emailClaim" name="emailClaim" placeholder="email" defaultValue={provider?.email_claim ?? "email"} maxLength={100} pattern="[A-Za-z0-9_:./\-]+" autoComplete="off" className="font-mono" /></Field>
        </div>
      </FieldSet></div>
      <Field><FieldLabel htmlFor="autoProvisionRole">First sign-in</FieldLabel><Select name="autoProvisionRole" defaultValue={provider?.auto_provision_role ?? "none"} items={autoProvisionRoles.map(({ value, label }) => ({ value, label }))} onValueChange={markDirty}><SelectTrigger id="autoProvisionRole" className="w-full"><SelectValue /></SelectTrigger><SelectContent>{autoProvisionRoles.map((role) => <SelectItem key={role.value} value={role.value}>{role.label}</SelectItem>)}</SelectContent></Select><FieldDescription>Grant a verified person with no membership this role on their first sign-in, or add each person under Members.</FieldDescription></Field>
    </FieldGroup>}
  </FormDrawer>
}

export function RemoveIdentityProviderDialog({ issuer, disabled = false }: { issuer: string; disabled?: boolean }) {
  const [open, setOpen] = useState(false)
  const [state, action, pending] = useActionState<IdentityProviderActionState, FormData>(removeIdentityProvider, {})
  // On success the page revalidates without a provider, which unmounts this dialog.
  return <AlertDialog open={open} onOpenChange={(next) => { if (!pending) setOpen(next) }}>
    <AlertDialogTrigger render={<Button variant="outline" disabled={disabled} />}><Trash2Icon /> Remove</AlertDialogTrigger>
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogMedia className="bg-destructive/10 text-destructive"><Trash2Icon /></AlertDialogMedia>
        <AlertDialogTitle>Remove this identity provider?</AlertDialogTitle>
        <AlertDialogDescription>Fabric stops accepting sign-ins from <span className="break-all font-mono text-xs">{issuer}</span> for this account. People keep their memberships, and tokens already issued stay valid until they expire.</AlertDialogDescription>
      </AlertDialogHeader>
      {state.error ? <p role="alert" className="text-sm text-destructive">{state.error}</p> : null}
      <form action={action}>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <Button type="submit" variant="destructive-solid" disabled={pending}>{pending ? <Spinner /> : <Trash2Icon />}{pending ? "Removing…" : "Remove provider"}</Button>
        </AlertDialogFooter>
      </form>
    </AlertDialogContent>
  </AlertDialog>
}
