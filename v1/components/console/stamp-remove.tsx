"use client"

import { useActionState, useState } from "react"
import { Trash2Icon, TriangleAlertIcon } from "lucide-react"

import { removeStamp, type ActionState } from "@/app/(console)/actions"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Spinner } from "@/components/ui/spinner"

// Removing a stamp revokes its machine credentials through the control plane's existing
// DELETE. Destructive confirmations stay centered dialogs, like deleting a deployment.
export function RemoveStampButton({ stampId, name, disabled = false, size = "sm", variant = "outline", label = "Remove" }: {
  stampId: string
  name: string
  disabled?: boolean
  size?: "xs" | "sm" | "default"
  variant?: "outline" | "destructive" | "ghost"
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const [state, action, pending] = useActionState(removeStamp.bind(null, stampId), {} as ActionState)

  // Closed once the removal succeeded; the page re-renders from the server without the
  // stamp's Remove action, so the dialog is never needed again.
  return <Dialog open={open && !state.ok} onOpenChange={(next) => { if (!pending) setOpen(next) }}>
    <DialogTrigger render={<Button type="button" size={size} variant={variant} disabled={disabled}><Trash2Icon />{label}</Button>} />
    <DialogContent showCloseButton={!pending}>
      <DialogHeader>
        <DialogTitle>Remove {name}?</DialogTitle>
        <DialogDescription>The stamp is removed from this account. Its activity log is kept.</DialogDescription>
      </DialogHeader>
      <Alert variant="destructive">
        <TriangleAlertIcon />
        <AlertTitle>What happens</AlertTitle>
        <AlertDescription>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            <li>Its agent and telemetry credentials are revoked immediately.</li>
            <li>The agent stops syncing: no heartbeats, desired state, status or usage from this machine.</li>
            <li>Deployments placed here are kept and marked <span className="font-medium">Stamp removed</span>. Move them to another stamp or delete them; nothing serves them until you do.</li>
            <li>Model hosts already running on the machine stay under its owner&apos;s control. To enroll it again, create a new enrollment token.</li>
          </ul>
        </AlertDescription>
      </Alert>
      <form action={action}>
        {state.error ? <p className="mb-3 text-xs text-destructive" role="alert">{state.error}</p> : null}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={pending} onClick={() => setOpen(false)}>Cancel</Button>
          <Button type="submit" variant="destructive" disabled={pending}>{pending ? <Spinner /> : <Trash2Icon />}{pending ? "Removing…" : "Remove stamp"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
