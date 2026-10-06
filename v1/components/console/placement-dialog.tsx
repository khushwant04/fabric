"use client"

import { useActionState, useState } from "react"
import { CheckIcon, RocketIcon } from "lucide-react"

import { assignDeployment, type ActionState } from "@/app/(console)/actions"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"

export function PlacementDialog({ deploymentId, stamps, disabled = false }: {
  deploymentId: string
  stamps: { id: string; name: string }[]
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [state, action, pending] = useActionState(assignDeployment.bind(null, deploymentId), {} as ActionState)
  return <Dialog open={open} onOpenChange={(next) => { if (!pending) setOpen(next) }}>
    <DialogTrigger render={<Button disabled={disabled}><RocketIcon /> Assign infrastructure</Button>} />
    <DialogContent showCloseButton={!pending}>
      <DialogHeader><DialogTitle>Assign infrastructure</DialogTitle><DialogDescription>Start the saved model deployment when compatible GPU capacity is available.</DialogDescription></DialogHeader>
      {state.ok ? <Alert><CheckIcon /><AlertTitle>Placement accepted</AlertTitle><AlertDescription>The operator will start the model host. Readiness and the inference URL update as the workload comes online.</AlertDescription></Alert> : <form action={action} className="space-y-5">
        <Field><FieldLabel>Infrastructure</FieldLabel><Select name="stampId" defaultValue="auto" disabled={pending}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="auto">Automatic placement</SelectItem>{stamps.map((stamp) => <SelectItem key={stamp.id} value={stamp.id}>{stamp.name}</SelectItem>)}</SelectContent></Select><FieldDescription>The control plane verifies ownership, GPU requirements, and free capacity.</FieldDescription>{state.error ? <FieldError>{state.error}</FieldError> : null}</Field>
        <DialogFooter><Button variant="outline" type="button" disabled={pending} onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={pending}>{pending ? <Spinner /> : <RocketIcon />}{pending ? "Assigning…" : "Assign model"}</Button></DialogFooter>
      </form>}
    </DialogContent>
  </Dialog>
}
