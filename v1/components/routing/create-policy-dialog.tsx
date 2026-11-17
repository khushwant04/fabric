"use client"

import { useActionState, useState } from "react"
import { PlusIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Spinner } from "@/components/ui/spinner"
import { createRoutingPolicy } from "@/lib/fabric/routing-actions"
import { aliasProblem, type DeploymentOption } from "@/lib/fabric/routing-policy"

export function CreatePolicyDialog({ deployments, disabled = false }: { deployments: DeploymentOption[]; disabled?: boolean }) {
  const [open, setOpen] = useState(false)
  const [alias, setAlias] = useState("")
  const [state, action, pending] = useActionState(createRoutingPolicy, {})
  const problem = alias ? aliasProblem(alias, deployments.map((item) => item.alias)) : null
  return <Dialog open={open} onOpenChange={(next) => { if (!pending) setOpen(next) }}>
    <DialogTrigger render={<Button disabled={disabled || !deployments.length}><PlusIcon /> New policy</Button>} />
    <DialogContent showCloseButton={!pending}>
      <DialogHeader>
        <DialogTitle>New routing policy</DialogTitle>
        <DialogDescription>Clients call the policy by its name. It starts as a draft that sends everything to one deployment; nothing changes until you publish.</DialogDescription>
      </DialogHeader>
      <form action={action} className="space-y-5">
        <FieldGroup>
          <Field data-invalid={problem ? true : undefined}>
            <FieldLabel htmlFor="policy-alias">Model name</FieldLabel>
            <Input id="policy-alias" name="alias" value={alias} onChange={(event) => setAlias(event.target.value.trim())} placeholder="chat-default" autoComplete="off" className="font-mono" aria-invalid={problem ? true : undefined} required disabled={pending} />
            {problem ? <FieldError>{problem}</FieldError> : <FieldDescription>Sent as <span className="font-mono">model</span> in OpenAI and Anthropic requests.</FieldDescription>}
          </Field>
          <Field>
            <FieldLabel htmlFor="policy-default">Default deployment</FieldLabel>
            <NativeSelect id="policy-default" name="deployment_id" className="w-full" required disabled={pending} defaultValue={deployments[0]?.id}>
              {deployments.map((item) => <NativeSelectOption key={item.id} value={item.id}>{item.name} ({item.alias})</NativeSelectOption>)}
            </NativeSelect>
          </Field>
          <Field>
            <FieldLabel htmlFor="policy-description">Description</FieldLabel>
            <Input id="policy-description" name="description" placeholder="Optional" maxLength={500} disabled={pending} />
          </Field>
        </FieldGroup>
        {state.error ? <FieldError>{state.error}</FieldError> : null}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={pending} onClick={() => setOpen(false)}>Cancel</Button>
          <Button type="submit" disabled={pending || Boolean(problem) || !alias}>{pending ? <Spinner /> : <PlusIcon />}{pending ? "Creating…" : "Create draft"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
