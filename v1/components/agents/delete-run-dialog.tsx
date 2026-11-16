"use client"

import { useActionState, useState } from "react"
import { Trash2Icon } from "lucide-react"

import { deleteAgentRun, type DeleteRunState } from "@/app/(console)/agents/runs/actions"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogMedia, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"

export function DeleteRunDialog({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false)
  const [state, action, pending] = useActionState<DeleteRunState, FormData>(deleteAgentRun.bind(null, runId), {})
  return <AlertDialog open={open} onOpenChange={(next) => { if (!pending) setOpen(next) }}>
    <AlertDialogTrigger render={<Button variant="outline" />}><Trash2Icon /> Delete run</AlertDialogTrigger>
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogMedia className="bg-destructive/10 text-destructive"><Trash2Icon /></AlertDialogMedia>
        <AlertDialogTitle>Delete this run?</AlertDialogTitle>
        <AlertDialogDescription>The gateway forgets the trace, its totals and its budget. A later request with the same run id starts a new run with a fresh budget. Usage records are not affected.</AlertDialogDescription>
      </AlertDialogHeader>
      {state.error ? <p role="alert" className="text-sm text-destructive">{state.error}</p> : null}
      <form action={action}>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <Button type="submit" variant="destructive-solid" disabled={pending}>{pending ? <Spinner /> : <Trash2Icon />}{pending ? "Deleting…" : "Delete run"}</Button>
        </AlertDialogFooter>
      </form>
    </AlertDialogContent>
  </AlertDialog>
}
