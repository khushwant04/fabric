"use client"

import { CheckIcon, RocketIcon } from "lucide-react"

import { assignDeployment, type ActionState } from "@/app/(console)/actions"
import { FormDrawer } from "@/components/console/form-drawer"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

export function PlacementDrawer({ deploymentId, stamps, disabled = false, label = "Assign infrastructure" }: {
  deploymentId: string
  stamps: { id: string; name: string }[]
  disabled?: boolean
  /** "Redeploy" when the deployment's stamp was removed. */
  label?: string
}) {
  return <FormDrawer<ActionState>
    trigger={{ label, icon: RocketIcon, disabled }}
    title="Assign infrastructure"
    description="Start the saved model deployment when compatible GPU capacity is available."
    icon={RocketIcon}
    action={assignDeployment.bind(null, deploymentId)}
    submitLabel="Assign model"
    pendingLabel="Assigning…"
    submitIcon={RocketIcon}
    renderSuccess={() => <Alert><CheckIcon /><AlertTitle>Placement accepted</AlertTitle><AlertDescription>The operator will start the model host. Readiness and the inference URL update as the workload comes online.</AlertDescription></Alert>}
  >
    {({ markDirty }) => <Field>
      <FieldLabel htmlFor="placement-stamp">Infrastructure</FieldLabel>
      <Select name="stampId" defaultValue="auto" items={[{ value: "auto", label: "Automatic placement" }, ...stamps.map((stamp) => ({ value: stamp.id, label: stamp.name }))]} onValueChange={markDirty}><SelectTrigger id="placement-stamp" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="auto">Automatic placement</SelectItem>{stamps.map((stamp) => <SelectItem key={stamp.id} value={stamp.id}>{stamp.name}</SelectItem>)}</SelectContent></Select>
      <FieldDescription>The control plane verifies ownership, GPU requirements, and free capacity.</FieldDescription>
    </Field>}
  </FormDrawer>
}
