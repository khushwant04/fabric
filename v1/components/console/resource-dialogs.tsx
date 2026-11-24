"use client"

import { useActionState, useState } from "react"
import { BotIcon, CheckIcon, CopyIcon, KeyRoundIcon, PlusIcon, Trash2Icon, UserPlusIcon } from "lucide-react"

import {
  addMember,
  createApiKey,
  createServicePrincipal,
  type ActionState,
} from "@/app/(console)/actions"
import { FormDrawer } from "@/components/console/form-drawer"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { confirmDeleteDeployment } from "@/lib/fabric/deployment-actions"
import { confirmsModelDeletion } from "@/lib/fabric/resource-state"
import type { ServicePrincipal } from "@/lib/fabric/types"

// Destructive confirmations stay centered dialogs; create forms open in drawers.
export function DeleteDeploymentDialog({ deploymentId, name, modelAlias, gpuCount, gpuClass, disabled = false }: {
  deploymentId: string
  name: string
  modelAlias: string
  gpuCount: number
  gpuClass: string
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState("")
  const [state, action, pending] = useActionState(confirmDeleteDeployment.bind(null, deploymentId), {})
  return <Dialog open={open} onOpenChange={(next) => { if (!pending) { setOpen(next); if (!next) setTyped("") } }}>
    <DialogTrigger render={<Button variant="destructive" disabled={disabled}><Trash2Icon /> Delete</Button>} />
    <DialogContent showCloseButton={!pending}>
      <DialogHeader><DialogTitle>Delete {name}?</DialogTitle><DialogDescription>This removes the model deployment and its placements from serving.</DialogDescription></DialogHeader>
      <Alert variant="destructive"><Trash2Icon /><AlertTitle>Serving and GPU impact</AlertTitle><AlertDescription>New inference requests will stop reaching this deployment as deletion is reconciled. Its model hosts will drain and shut down. {gpuCount > 0 ? <>The requested {gpuCount} {gpuClass.toUpperCase()} GPU{gpuCount === 1 ? "" : "s"} will be released after cleanup is confirmed.</> : <>GPU capacity is released after model-host cleanup is confirmed.</>} Cleanup waits if a stamp is unreachable. Usage history is retained.</AlertDescription></Alert>
      <form action={action} className="space-y-5">
        <Field><FieldLabel htmlFor={`delete-${deploymentId}`}>Type the model alias to confirm</FieldLabel><FieldDescription><span className="break-all font-mono">{modelAlias}</span></FieldDescription><Input id={`delete-${deploymentId}`} name="confirmation" value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" disabled={pending} required />{state.error ? <FieldError>{state.error}</FieldError> : null}</Field>
        <DialogFooter><Button type="button" variant="outline" disabled={pending} onClick={() => { setTyped(""); setOpen(false) }}>Cancel</Button><Button type="submit" variant="destructive-solid" disabled={pending || !confirmsModelDeletion(typed, modelAlias)}>{pending ? <Spinner /> : <Trash2Icon />}{pending ? "Deleting…" : "Delete deployment"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function SavedMessage({ title, children }: { title: string; children: React.ReactNode }) {
  return <Alert>
    <CheckIcon />
    <AlertTitle>{title}</AlertTitle>
    <AlertDescription>{children}</AlertDescription>
  </Alert>
}

function CopySecret({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState("")
  return (
    <Field>
      <FieldLabel htmlFor="api-key-secret">Secret</FieldLabel>
      <div className="flex gap-2">
        <Input id="api-key-secret" readOnly value={value} className="font-mono text-xs" onFocus={(event) => event.currentTarget.select()} />
        <Button
          type="button"
          variant="outline"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(value)
              setCopied(true)
              setError("")
              window.setTimeout(() => setCopied(false), 1500)
            } catch {
              setError("Copy is unavailable. Select the secret and copy it manually.")
            }
          }}
          aria-label="Copy secret"
        >
          {copied ? <CheckIcon /> : <CopyIcon />}{copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <span role="status" className="sr-only">{copied ? "Secret copied to the clipboard" : ""}</span>
      {error ? <FieldError>{error}</FieldError> : null}
    </Field>
  )
}

const roles = ["viewer", "developer", "admin", "owner"]

export function AddMemberDrawer({ disabled = false }: { disabled?: boolean }) {
  return <FormDrawer<ActionState>
    trigger={{ label: "Add member", icon: UserPlusIcon, disabled }}
    title="Add account member"
    description="Add or reactivate a person using their Auth0 subject."
    icon={UserPlusIcon}
    action={addMember}
    submitLabel="Add member"
    pendingLabel="Adding…"
    renderSuccess={() => <SavedMessage title="Member saved">The control plane accepted this change.</SavedMessage>}
  >
    {({ markDirty }) => <FieldGroup>
      <Field><FieldLabel htmlFor="subject">Auth0 subject</FieldLabel><Input id="subject" name="subject" placeholder="auth0|user-id" required autoComplete="off" /></Field>
      <Field><FieldLabel htmlFor="email">Email (optional)</FieldLabel><Input id="email" name="email" type="email" placeholder="operator@example.com" /></Field>
      <Field><FieldLabel htmlFor="member-role">Role</FieldLabel><Select name="role" defaultValue="viewer" items={roles.map((role) => ({ value: role, label: role[0].toUpperCase() + role.slice(1) }))} onValueChange={markDirty}><SelectTrigger id="member-role" className="w-full"><SelectValue /></SelectTrigger><SelectContent>{roles.map((role) => <SelectItem value={role} key={role} className="capitalize">{role}</SelectItem>)}</SelectContent></Select><FieldDescription>Roles determine the scopes issued when the member starts a console session.</FieldDescription></Field>
    </FieldGroup>}
  </FormDrawer>
}

export function CreatePrincipalDrawer({ disabled = false }: { disabled?: boolean }) {
  return <FormDrawer<ActionState>
    trigger={{ label: "Create principal", icon: PlusIcon, disabled }}
    title="Create service principal"
    description="Create an automation identity that can own API keys."
    icon={BotIcon}
    action={createServicePrincipal}
    submitLabel="Create principal"
    pendingLabel="Creating…"
    renderSuccess={() => <SavedMessage title="Service principal created">The control plane accepted this change. API keys can now be issued to this principal.</SavedMessage>}
  >
    {() => <FieldGroup>
      <Field><FieldLabel htmlFor="principalName">Name</FieldLabel><Input id="principalName" name="name" placeholder="production-gateway" required autoComplete="off" /><FieldDescription>A recognizable name for the gateway, job or CI system that will use it.</FieldDescription></Field>
    </FieldGroup>}
  </FormDrawer>
}

const delegatedScopes = [
  "accounts:read", "members:read", "api-keys:read", "deployments:read",
  "deployments:write", "stamps:read", "inference:invoke",
]

// The secret lives only in this drawer session's action state: it is shown once, and the
// session (with its state) is discarded when the drawer closes.
export function CreateApiKeyDrawer({ principals, disabled = false }: { principals: ServicePrincipal[]; disabled?: boolean }) {
  return <FormDrawer<ActionState>
    trigger={{ label: "Create API key", icon: PlusIcon, disabled }}
    title="Create API key"
    description="Issue a scoped credential for a person or service principal."
    icon={KeyRoundIcon}
    action={createApiKey}
    submitLabel="Create key"
    pendingLabel="Creating…"
    submitIcon={KeyRoundIcon}
    renderSuccess={(state) => state.secret ? <FieldGroup>
      <Alert><KeyRoundIcon /><AlertTitle>Copy your key now</AlertTitle><AlertDescription>Fabric stores only a verifier. This secret cannot be recovered later, and closing this drawer clears it.</AlertDescription></Alert>
      <CopySecret value={state.secret} />
    </FieldGroup> : <SavedMessage title="Key created">The control plane accepted this change.</SavedMessage>}
  >
    {({ markDirty }) => <FieldGroup>
      <Field><FieldLabel htmlFor="keyName">Name</FieldLabel><Input id="keyName" name="name" placeholder="CI deployment key" required autoComplete="off" /></Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field><FieldLabel htmlFor="keyOwner">Owner</FieldLabel><Select name="servicePrincipalId" defaultValue="human" items={[{ value: "human", label: "Current user" }, ...principals.filter((item) => item.status === "active").map((item) => ({ value: item.id, label: item.name }))]} onValueChange={markDirty}><SelectTrigger id="keyOwner" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="human">Current user</SelectItem>{principals.filter((item) => item.status === "active").map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="expiresInDays">Expires in days</FieldLabel><Input id="expiresInDays" name="expiresInDays" type="number" min={1} max={365} placeholder="No expiry" /></Field>
      </div>
      <Field><FieldLabel id="keyScopes">Scopes</FieldLabel><div role="group" aria-labelledby="keyScopes" className="grid gap-2 rounded-md border border-border p-3 sm:grid-cols-2">{delegatedScopes.map((scope) => <label className="flex items-center gap-2 font-mono text-xs" key={scope}><Checkbox name="scopes" value={scope} defaultChecked={scope === "deployments:read"} onCheckedChange={markDirty} />{scope}</label>)}</div><FieldDescription>Grant only the scopes this credential needs.</FieldDescription></Field>
    </FieldGroup>}
  </FormDrawer>
}
