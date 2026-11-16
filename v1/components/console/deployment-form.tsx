"use client"

import { useActionState, useState } from "react"
import { useFormStatus } from "react-dom"
import { PlusIcon, RocketIcon } from "lucide-react"

import { createDeployment, type ActionState } from "@/app/(console)/actions"
import { FormDrawer } from "@/components/console/form-drawer"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { balancingStrategies, capabilityFlags, cascadeTierRange, defaultCapabilities, kernelModes, priorityRange } from "@/lib/fabric/deployment-options"
import type { DeploymentStampOption } from "@/lib/fabric/resource-state"

const executionModes = [{ value: "default", label: "Cluster default" }, { value: "eager", label: "Eager" }, { value: "cuda_graph", label: "CUDA graphs" }]
const toolChoiceModes = [{ value: "default", label: "Model default" }, { value: "enabled", label: "Enabled" }, { value: "disabled", label: "Disabled" }]
const gpuClasses = ["t4", "v100", "rtx-a4000", "l4", "a10", "a10g", "a100", "a100-80gb", "l40s", "h100"]

/** The full-page form at /deployments/new (deep links and no-JS submissions). */
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

/**
 * The create-deployment fields, shared by the page and the drawer. Columns follow the
 * width of the surrounding container, so the same fields fit a page card and a drawer.
 */
export function DeploymentFields({ stamps, disabled = false, onSelectChange }: {
  stamps: DeploymentStampOption[]
  disabled?: boolean
  onSelectChange?: () => void
}) {
  const [kernelMode, setKernelMode] = useState<string>("auto")
  const [strategy, setStrategy] = useState<string>("least_in_flight")
  return <div className="@container">
    <FieldGroup>
      <Field><FieldLabel htmlFor="modelRef">Model repository</FieldLabel><Input id="modelRef" name="modelRef" placeholder="Qwen/Qwen3.5-4B" required maxLength={200} disabled={disabled} autoComplete="off" /><FieldDescription>The model-host image must support this model architecture. Gated models need a download credential configured on the cluster.</FieldDescription></Field>
      <div className="grid gap-5 @lg:grid-cols-2">
        <Field><FieldLabel htmlFor="name">Deployment name</FieldLabel><Input id="name" name="name" placeholder="qwen-4b" pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?" maxLength={200} required disabled={disabled} autoComplete="off" /></Field>
        <Field><FieldLabel htmlFor="modelAlias">API model name</FieldLabel><Input id="modelAlias" name="modelAlias" placeholder="qwen3.5-4b" pattern="[a-zA-Z0-9_.-]+" maxLength={200} required disabled={disabled} autoComplete="off" /><FieldDescription>Use this name in the OpenAI request’s model field.</FieldDescription></Field>
      </div>
      <Field><FieldLabel htmlFor="stampId">Infrastructure</FieldLabel><Select name="stampId" defaultValue="auto" items={[{ value: "auto", label: "Automatic placement" }, ...stamps.map((stamp) => ({ value: stamp.id, label: stamp.label }))]} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="stampId" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="auto">Automatic placement</SelectItem>{stamps.map((stamp) => <SelectItem key={stamp.id} value={stamp.id}>{stamp.label}</SelectItem>)}</SelectContent></Select><FieldDescription>{stamps.length ? "Only recently connected clusters appear here. The control plane checks available capacity before assigning the model." : "No active account-owned cluster is visible. Enroll a stamp first, or use entitled managed capacity through automatic placement."}</FieldDescription></Field>
      <div className="grid gap-5 @lg:grid-cols-3">
        <Field><FieldLabel htmlFor="replicas">Replicas</FieldLabel><Input id="replicas" name="replicas" type="number" min={1} max={32} defaultValue={1} required disabled={disabled} /></Field>
        <Field><FieldLabel htmlFor="gpuCount">GPUs per replica</FieldLabel><Input id="gpuCount" name="gpuCount" type="number" min={1} max={8} defaultValue={1} required disabled={disabled} /><FieldDescription>All GPUs for one replica must fit on one node.</FieldDescription></Field>
        <Field><FieldLabel htmlFor="gpuClass">Minimum GPU class</FieldLabel><Select name="gpuClass" defaultValue="t4" items={gpuClasses.map((value) => ({ value, label: value.toUpperCase() }))} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="gpuClass" className="w-full"><SelectValue /></SelectTrigger><SelectContent>{gpuClasses.map((value) => <SelectItem value={value} key={value}>{value.toUpperCase()}</SelectItem>)}</SelectContent></Select></Field>
      </div>
      <details className="group rounded-xl border border-border/70 p-4"><summary className="cursor-pointer rounded-md text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">Serving settings</summary><div className="mt-5 grid gap-5 @lg:grid-cols-3">
        <Field><FieldLabel htmlFor="maxModelLen">Context limit</FieldLabel><Input id="maxModelLen" name="maxModelLen" type="number" min={64} max={1048576} placeholder="Cluster default" disabled={disabled} /></Field>
        <Field><FieldLabel htmlFor="maxNumSeqs">Concurrent sequences</FieldLabel><Input id="maxNumSeqs" name="maxNumSeqs" type="number" min={1} max={1024} placeholder="Cluster default" disabled={disabled} /></Field>
        <Field><FieldLabel htmlFor="execution">Execution</FieldLabel><Select name="execution" defaultValue="default" items={executionModes} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="execution" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">Cluster default</SelectItem><SelectItem value="eager">Eager</SelectItem><SelectItem value="cuda_graph">CUDA graphs</SelectItem></SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="autoToolChoice">Automatic tool calling</FieldLabel><Select name="autoToolChoice" defaultValue="default" items={toolChoiceModes} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="autoToolChoice" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">Model default</SelectItem><SelectItem value="enabled">Enabled</SelectItem><SelectItem value="disabled">Disabled</SelectItem></SelectContent></Select><FieldDescription>Allow the model to select tools supplied in an API request.</FieldDescription></Field>
        <Field className="@lg:col-span-3"><FieldLabel htmlFor="kernelMode">Kernel mode</FieldLabel><Select name="kernelMode" value={kernelMode} items={kernelModes.map(({ value, label }) => ({ value, label }))} disabled={disabled} onValueChange={(value) => { setKernelMode(String(value)); onSelectChange?.() }}><SelectTrigger id="kernelMode" className="w-full @lg:w-1/3"><SelectValue /></SelectTrigger><SelectContent>{kernelModes.map((mode) => <SelectItem key={mode.value} value={mode.value}>{mode.label}</SelectItem>)}</SelectContent></Select><FieldDescription>{kernelModes.find((mode) => mode.value === kernelMode)?.description}</FieldDescription></Field>
        <Field className="@lg:col-span-2"><FieldLabel htmlFor="toolCallParser">Tool parser</FieldLabel><Input id="toolCallParser" name="toolCallParser" placeholder="Model default" maxLength={64} pattern="[a-z][a-z0-9_-]*" disabled={disabled} autoComplete="off" /><FieldDescription>Known Qwen models select a compatible parser automatically. Use hermes for Qwen3-VL or qwen3_xml for Qwen3.5. Other models need a parser supported by the model-host image.</FieldDescription></Field>
      </div><p className="mt-4 text-xs text-muted-foreground">The operator selects a supported dtype from the GPU profile. Model memory fit still depends on model size, context, and serving settings.</p></details>
      <details className="group rounded-xl border border-border/70 p-4" data-section="routing"><summary className="cursor-pointer rounded-md text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">Routing</summary><div className="mt-5 grid gap-5">
        <input type="hidden" name="routing" value="1" />
        <Field><FieldLabel htmlFor="strategy">Balancing strategy</FieldLabel><Select name="strategy" value={strategy} items={balancingStrategies.map(({ value, label }) => ({ value, label }))} disabled={disabled} onValueChange={(value) => { setStrategy(String(value)); onSelectChange?.() }}><SelectTrigger id="strategy" className="w-full @lg:w-1/2"><SelectValue /></SelectTrigger><SelectContent>{balancingStrategies.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent></Select><FieldDescription>How the gateway spreads requests across this deployment’s replicas. {balancingStrategies.find((option) => option.value === strategy)?.description}</FieldDescription></Field>
        <Field><FieldLabel id="capabilities-label">Capabilities</FieldLabel><div role="group" aria-labelledby="capabilities-label" className="grid gap-2 rounded-xl border border-border/70 p-3 @md:grid-cols-2 @lg:grid-cols-4">{capabilityFlags.map((flag) => <label key={flag.value} className="flex items-start gap-2 text-sm" title={flag.description}><Checkbox name="capabilities" value={flag.value} defaultChecked={defaultCapabilities.includes(flag.value)} disabled={disabled} onCheckedChange={onSelectChange} className="mt-0.5" /><span className="min-w-0"><span className="block leading-5">{flag.label}</span><span className="block text-xs text-muted-foreground">{flag.description}</span></span></label>)}</div><FieldDescription>What this model can serve. With <code className="font-mono text-xs">model: &quot;auto&quot;</code>, the gateway picks among the account’s deployments whose capabilities fit the request.</FieldDescription></Field>
        <label className="flex items-start gap-2 text-sm"><Checkbox name="autoEnabled" defaultChecked disabled={disabled} onCheckedChange={onSelectChange} className="mt-0.5" /><span><span className="block leading-5">Eligible for automatic routing</span><span className="block text-xs text-muted-foreground">Clear to serve this deployment only by its API model name.</span></span></label>
        <div className="grid gap-5 @lg:grid-cols-2">
          <Field><FieldLabel htmlFor="priority">Priority</FieldLabel><Input id="priority" name="priority" type="number" min={priorityRange.min} max={priorityRange.max} step={1} defaultValue={0} disabled={disabled} /><FieldDescription>Breaks ties between deployments that fit an automatic request equally well; higher wins. From {priorityRange.min} to {priorityRange.max}.</FieldDescription></Field>
          <Field><FieldLabel htmlFor="cascadeTier">Cascade tier</FieldLabel><Input id="cascadeTier" name="cascadeTier" type="number" min={cascadeTierRange.min} max={cascadeTierRange.max} step={1} placeholder="Not in an explicit ladder" disabled={disabled} /><FieldDescription>Optional. With <code className="font-mono text-xs">model: &quot;cascade&quot;</code>, tiered deployments are tried lowest tier first and escalate when an answer fails its checks. Without any tiers, the ladder follows priority, lowest first.</FieldDescription></Field>
        </div>
      </div></details>
    </FieldGroup>
  </div>
}

export function DeploymentNextSteps() {
  return <>
    <ol className="list-decimal space-y-3 pl-4 text-sm text-muted-foreground"><li>The control plane validates and assigns the deployment.</li><li>The stamp agent delivers it to the operator.</li><li>The operator starts the model host on a compatible GPU node.</li><li>The gateway routes requests when the host is ready.</li></ol>
    <p className="text-xs text-muted-foreground">The inference URL appears on the deployment when the stamp reports readiness. A single-GPU node can start one single-GPU model; replacing an occupied model requires spare capacity or stopping it first.</p>
  </>
}

/**
 * "Create deployment" opens this drawer in place. The trigger is a link to
 * /deployments/new, so modified clicks and visits without JavaScript use the full page.
 * On success the server action redirects to the new deployment.
 */
export function CreateDeploymentDrawer({ stamps, infrastructureUnavailable = false, disabled = false }: {
  stamps: DeploymentStampOption[]
  /** The stamp list could not be read: deploying is blocked, as on the full page. */
  infrastructureUnavailable?: boolean
  disabled?: boolean
}) {
  return <FormDrawer<ActionState>
    trigger={{ label: "Create deployment", icon: PlusIcon, disabled, href: "/deployments/new" }}
    title="Deploy a model"
    description="Choose a model and enrolled infrastructure. Fabric creates and manages its GPU workload."
    icon={RocketIcon}
    size="wide"
    action={createDeployment}
    submitLabel="Deploy model"
    pendingLabel="Creating deployment…"
    submitIcon={RocketIcon}
    submitDisabled={infrastructureUnavailable}
  >
    {({ markDirty }) => <div className="space-y-6">
      {infrastructureUnavailable ? <Alert variant="destructive"><RocketIcon /><AlertTitle>Infrastructure could not be checked</AlertTitle><AlertDescription>Refresh before deploying. No capacity has been assumed available.</AlertDescription></Alert> : null}
      <DeploymentFields stamps={stamps} onSelectChange={markDirty} />
      <section aria-labelledby="deployment-next-steps" className="space-y-4 rounded-xl bg-muted/40 p-4">
        <h3 id="deployment-next-steps" className="text-sm font-medium">What happens next</h3>
        <DeploymentNextSteps />
        <p className="text-xs text-muted-foreground">Authentication, TLS, and gateway settings come from your Helm installation.</p>
      </section>
    </div>}
  </FormDrawer>
}
