"use client"

import { useActionState } from "react"
import { useFormStatus } from "react-dom"
import { RocketIcon } from "lucide-react"

import { createDeployment, type ActionState } from "@/app/(console)/actions"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"

export function DeploymentForm({ children }: { children: React.ReactNode }) {
  const [state, action, pending] = useActionState(createDeployment, {} as ActionState)
  return <form action={action}>
    {state.error ? <Alert variant="destructive" className="mb-5"><RocketIcon /><AlertTitle>Deployment could not be created</AlertTitle><AlertDescription>{state.error}</AlertDescription></Alert> : null}
    <fieldset disabled={pending} className="grid gap-5 xl:grid-cols-[1fr_320px]">{children}</fieldset>
  </form>
}

export function DeployModelSubmit({ disabled }: { disabled: boolean }) {
  const { pending } = useFormStatus()
  return <Button className="w-full" type="submit" disabled={disabled || pending}>{pending ? <Spinner /> : <RocketIcon />}{pending ? "Creating deployment…" : "Deploy model"}</Button>
}
