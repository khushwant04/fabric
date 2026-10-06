"use client"

import { useActionState, useState } from "react"
import { CheckIcon, CopyIcon, KeyRoundIcon, PlusIcon, Trash2Icon, UserPlusIcon } from "lucide-react"

import {
  addMember,
  createApiKey,
  createEnrollmentToken,
  createServicePrincipal,
  type ActionState,
} from "@/app/(console)/actions"
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

const initialState: ActionState = {}

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
        <DialogFooter><Button type="button" variant="outline" disabled={pending} onClick={() => { setTyped(""); setOpen(false) }}>Cancel</Button><Button type="submit" variant="destructive" disabled={pending || !confirmsModelDeletion(typed, modelAlias)}>{pending ? <Spinner /> : <Trash2Icon />}{pending ? "Deleting…" : "Delete deployment"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function SubmitButton({ pending, children }: { pending: boolean; children: React.ReactNode }) {
  return (
    <Button type="submit" disabled={pending}>
      {pending ? <Spinner /> : null}
      {children}
    </Button>
  )
}

function ResultMessage({ state }: { state: ActionState }) {
  if (state.error) return <FieldError>{state.error}</FieldError>
  if (state.ok && !state.secret && !state.enrollmentToken) {
    return (
      <Alert>
        <CheckIcon />
        <AlertTitle>Saved</AlertTitle>
        <AlertDescription>The control plane accepted this change.</AlertDescription>
      </Alert>
    )
  }
  return null
}

function CopySecret({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex gap-2">
      <Input readOnly value={value} className="font-mono text-xs" />
      <Button
        type="button"
        variant="outline"
        size="icon"
        onClick={async () => {
          await navigator.clipboard.writeText(value)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
        aria-label="Copy secret"
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </Button>
    </div>
  )
}

export function EnrollmentTokenDialog({ disabled = false }: { disabled?: boolean }) {
  const [state, action, pending] = useActionState(createEnrollmentToken, initialState)
  return (
    <Dialog>
      <DialogTrigger render={<Button disabled={disabled}><PlusIcon /> Enroll stamp</Button>} />
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create enrollment token</DialogTitle>
          <DialogDescription>Generate a short-lived, single-use credential for a new inference stamp.</DialogDescription>
        </DialogHeader>
        {state.enrollmentToken ? (
          <FieldGroup>
            <Alert>
              <KeyRoundIcon />
              <AlertTitle>Save this token now</AlertTitle>
              <AlertDescription>It is shown once and expires {state.expiresAt ? new Date(state.expiresAt).toLocaleString() : "soon"}.</AlertDescription>
            </Alert>
            <CopySecret value={state.enrollmentToken} />
          </FieldGroup>
        ) : (
          <form action={action} className="space-y-5">
            <FieldGroup>
              <Field>
                <FieldLabel>Stamp mode</FieldLabel>
                <Select name="mode" defaultValue="byoi">
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent><SelectItem value="byoi">Bring your own infrastructure</SelectItem></SelectContent>
                </Select>
              </Field>
              <Field>
                <FieldLabel htmlFor="expiresInMinutes">Expires in</FieldLabel>
                <Input id="expiresInMinutes" name="expiresInMinutes" type="number" min={5} max={1440} defaultValue={60} />
                <FieldDescription>Between 5 minutes and 24 hours.</FieldDescription>
              </Field>
              <ResultMessage state={state} />
            </FieldGroup>
            <DialogFooter><SubmitButton pending={pending}>Generate token</SubmitButton></DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

export function AddMemberDialog({ disabled = false }: { disabled?: boolean }) {
  const [state, action, pending] = useActionState(addMember, initialState)
  return (
    <Dialog>
      <DialogTrigger render={<Button disabled={disabled}><UserPlusIcon /> Add member</Button>} />
      <DialogContent>
        <DialogHeader><DialogTitle>Add account member</DialogTitle><DialogDescription>Add or reactivate a person using their Auth0 subject.</DialogDescription></DialogHeader>
        <form action={action} className="space-y-5">
          <FieldGroup>
            <Field><FieldLabel htmlFor="subject">Auth0 subject</FieldLabel><Input id="subject" name="subject" placeholder="auth0|user-id" required /></Field>
            <Field><FieldLabel htmlFor="email">Email (optional)</FieldLabel><Input id="email" name="email" type="email" placeholder="operator@example.com" /></Field>
            <Field><FieldLabel>Role</FieldLabel><Select name="role" defaultValue="viewer"><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent>{["viewer", "developer", "admin", "owner"].map((role) => <SelectItem value={role} key={role} className="capitalize">{role}</SelectItem>)}</SelectContent></Select></Field>
            <ResultMessage state={state} />
          </FieldGroup>
          <DialogFooter><SubmitButton pending={pending}>Add member</SubmitButton></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function CreatePrincipalDialog({ disabled = false }: { disabled?: boolean }) {
  const [state, action, pending] = useActionState(createServicePrincipal, initialState)
  return (
    <Dialog>
      <DialogTrigger render={<Button disabled={disabled}><PlusIcon /> Create principal</Button>} />
      <DialogContent>
        <DialogHeader><DialogTitle>Create service principal</DialogTitle><DialogDescription>Create an automation identity that can own API keys.</DialogDescription></DialogHeader>
        <form action={action} className="space-y-5">
          <FieldGroup><Field><FieldLabel htmlFor="principalName">Name</FieldLabel><Input id="principalName" name="name" placeholder="production-gateway" required /></Field><ResultMessage state={state} /></FieldGroup>
          <DialogFooter><SubmitButton pending={pending}>Create principal</SubmitButton></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

const delegatedScopes = [
  "accounts:read", "members:read", "api-keys:read", "deployments:read",
  "deployments:write", "stamps:read", "inference:invoke",
]

export function CreateApiKeyDialog({ principals, disabled = false }: { principals: ServicePrincipal[]; disabled?: boolean }) {
  const [state, action, pending] = useActionState(createApiKey, initialState)
  return (
    <Dialog>
      <DialogTrigger render={<Button disabled={disabled}><PlusIcon /> Create API key</Button>} />
      <DialogContent className="sm:max-w-xl">
        <DialogHeader><DialogTitle>Create API key</DialogTitle><DialogDescription>Issue a scoped credential for a person or service principal.</DialogDescription></DialogHeader>
        {state.secret ? (
          <FieldGroup><Alert><KeyRoundIcon /><AlertTitle>Copy your key now</AlertTitle><AlertDescription>Fabric stores only a verifier. This secret cannot be recovered later.</AlertDescription></Alert><CopySecret value={state.secret} /></FieldGroup>
        ) : (
          <form action={action} className="space-y-5">
            <FieldGroup>
              <Field><FieldLabel htmlFor="keyName">Name</FieldLabel><Input id="keyName" name="name" placeholder="CI deployment key" required /></Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field><FieldLabel>Owner</FieldLabel><Select name="servicePrincipalId" defaultValue="human"><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="human">Current user</SelectItem>{principals.filter((item) => item.status === "active").map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent></Select></Field>
                <Field><FieldLabel htmlFor="expiresInDays">Expires in days</FieldLabel><Input id="expiresInDays" name="expiresInDays" type="number" min={1} max={365} placeholder="No expiry" /></Field>
              </div>
              <Field><FieldLabel>Scopes</FieldLabel><div className="grid gap-2 rounded-md border p-3 sm:grid-cols-2">{delegatedScopes.map((scope) => <label className="flex items-center gap-2 text-xs" key={scope}><Checkbox name="scopes" value={scope} defaultChecked={scope === "deployments:read"} />{scope}</label>)}</div></Field>
              <ResultMessage state={state} />
            </FieldGroup>
            <DialogFooter><SubmitButton pending={pending}>Create key</SubmitButton></DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
