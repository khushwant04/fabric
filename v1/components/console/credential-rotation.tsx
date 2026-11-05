"use client"

import { useActionState } from "react"
import { KeyRoundIcon } from "lucide-react"

import { requestStampRotation, type ActionState } from "@/app/(console)/actions"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"

// Asks the stamp's agent to rotate its machine credentials. The agent performs the
// rotation on its next heartbeat; the old credentials keep working through an overlap.
export function CredentialRotationButton({ stampId, pending: requested, disabled = false }: {
  stampId: string
  pending: boolean
  disabled?: boolean
}) {
  const [state, action, submitting] = useActionState(requestStampRotation.bind(null, stampId), {} as ActionState)
  const waiting = requested || state.ok
  return <form action={action} className="flex flex-wrap items-center gap-2">
    <Button type="submit" size="sm" variant="outline" disabled={disabled || submitting || waiting}>
      {submitting ? <Spinner /> : <KeyRoundIcon />}{waiting ? "Rotation requested" : "Rotate credentials"}
    </Button>
    {waiting ? <span className="text-xs text-muted-foreground">The agent rotates on its next heartbeat.</span> : null}
    {state.error ? <span className="text-xs text-destructive">{state.error}</span> : null}
  </form>
}
