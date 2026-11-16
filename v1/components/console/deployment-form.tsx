"use client"

import Link from "next/link"
import { useActionState, useCallback, useState, type ReactNode } from "react"
import { useFormStatus } from "react-dom"
import { ArrowLeftIcon, BoxIcon, ChevronRightIcon, CpuIcon, PlusIcon, RocketIcon, WrenchIcon } from "lucide-react"

import { createDeployment, type ActionState } from "@/app/(console)/actions"
import { DrawerPanel, DrawerTriggerButton, FormDrawerBody, FormDrawerFooter, useFormDrawer, useGuardedClose } from "@/components/console/form-drawer"
import { IconTile } from "@/components/console/section"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { balancingStrategies, capabilityFlags, cascadeTierRange, defaultCapabilities, kernelModes, priorityRange } from "@/lib/fabric/deployment-options"
import { catalogModel, modelCatalog, stampFitsClass, suggestedDeploymentName, type CatalogModel, type DeploymentStampChoice } from "@/lib/fabric/model-catalog"
import { cn } from "@/lib/utils"

const executionModes = [{ value: "default", label: "Cluster default" }, { value: "eager", label: "Eager" }, { value: "cuda_graph", label: "CUDA graphs" }]
const toolChoiceModes = [{ value: "default", label: "Model default" }, { value: "enabled", label: "Enabled" }, { value: "disabled", label: "Disabled" }]
const gpuClasses = ["t4", "v100", "rtx-a4000", "l4", "a10", "a10g", "a100", "a100-80gb", "l40s", "h100", "rtx-pro-6000"]

/** "custom" opens the full form; a catalog id opens the short confirmation. */
export type ModelSelection = string | null

/** The full-page form at /deployments/new (deep links and no-JS submissions). */
export function DeploymentForm({ children }: { children: ReactNode }) {
  const [state, action, pending] = useActionState(createDeployment, {} as ActionState)
  return <form action={action}>
    {state.error ? <Alert variant="destructive" className="mb-5"><RocketIcon /><AlertTitle>Deployment could not be created</AlertTitle><AlertDescription>{state.error}</AlertDescription></Alert> : null}
    <fieldset disabled={pending} className="m-0 flex min-w-0 flex-col gap-6 border-0 p-0">{children}</fieldset>
  </form>
}

/** The big primary Deploy button; shows progress while its form submits. */
export function DeploySubmit({ disabled = false, className }: { disabled?: boolean; className?: string }) {
  const { pending } = useFormStatus()
  return <Button size="lg" type="submit" className={className} disabled={disabled || pending}>{pending ? <Spinner /> : <RocketIcon />}{pending ? "Creating deployment…" : "Deploy"}</Button>
}

function Disclosure({ title, section, children }: { title: string; section?: string; children: ReactNode }) {
  return <details className="group overflow-hidden rounded-xl border border-border" data-section={section}>
    <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium outline-none select-none hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset [&::-webkit-details-marker]:hidden"><ChevronRightIcon aria-hidden="true" className="size-3.5 text-muted-foreground transition-transform group-open:rotate-90" />{title}</summary>
    {children}
  </details>
}

/**
 * Every create-deployment field, shared by the page and the drawer. Columns follow the
 * width of the surrounding container. A catalog `preset` fills in the defaults; without
 * `withPlacement` the name and infrastructure fields are left to the caller.
 */
export function DeploymentFields({ stamps, disabled = false, onSelectChange, preset = null, withPlacement = true }: {
  stamps: DeploymentStampChoice[]
  disabled?: boolean
  onSelectChange?: () => void
  preset?: CatalogModel | null
  withPlacement?: boolean
}) {
  const [kernelMode, setKernelMode] = useState<string>("auto")
  const [strategy, setStrategy] = useState<string>("least_in_flight")
  return <div className="@container">
    <FieldGroup>
      <Field><FieldLabel htmlFor="modelRef">Model repository</FieldLabel><Input id="modelRef" name="modelRef" placeholder="Qwen/Qwen3.5-4B" defaultValue={preset?.modelRef} required maxLength={200} disabled={disabled} autoComplete="off" /><FieldDescription>The model-host image must support this model architecture. Gated models need a download credential configured on the cluster.</FieldDescription></Field>
      <div className="grid gap-5 @lg:grid-cols-2">
        {withPlacement ? <Field><FieldLabel htmlFor="name">Deployment name</FieldLabel><Input id="name" name="name" placeholder="qwen-4b" pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?" maxLength={200} required disabled={disabled} autoComplete="off" /></Field> : null}
        <Field><FieldLabel htmlFor="modelAlias">API model name</FieldLabel><Input id="modelAlias" name="modelAlias" placeholder="qwen3.5-4b" defaultValue={preset?.modelAlias} pattern="[a-zA-Z0-9_.-]+" maxLength={200} required disabled={disabled} autoComplete="off" /><FieldDescription>Use this name in the OpenAI request’s model field.</FieldDescription></Field>
      </div>
      {withPlacement ? <Field><FieldLabel htmlFor="stampId">Infrastructure</FieldLabel><Select name="stampId" defaultValue="auto" items={[{ value: "auto", label: "Automatic placement" }, ...stamps.map((stamp) => ({ value: stamp.id, label: stamp.label }))]} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="stampId" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="auto">Automatic placement</SelectItem>{stamps.map((stamp) => <SelectItem key={stamp.id} value={stamp.id}>{stamp.label}</SelectItem>)}</SelectContent></Select><FieldDescription>{stamps.length ? "Only recently connected clusters appear here. The control plane checks available capacity before assigning the model." : "No active account-owned cluster is visible. Enroll a stamp first, or use entitled managed capacity through automatic placement."}</FieldDescription></Field> : null}
      <div className="grid gap-5 @lg:grid-cols-3">
        <Field><FieldLabel htmlFor="replicas">Replicas</FieldLabel><Input id="replicas" name="replicas" type="number" min={1} max={32} defaultValue={1} required disabled={disabled} /></Field>
        <Field><FieldLabel htmlFor="gpuCount">GPUs per replica</FieldLabel><Input id="gpuCount" name="gpuCount" type="number" min={1} max={8} defaultValue={1} required disabled={disabled} /><FieldDescription>All GPUs for one replica must fit on one node.</FieldDescription></Field>
        <Field><FieldLabel htmlFor="gpuClass">Minimum GPU class</FieldLabel><Select name="gpuClass" defaultValue={preset?.gpuClass ?? "t4"} items={gpuClasses.map((value) => ({ value, label: value.toUpperCase() }))} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="gpuClass" className="w-full"><SelectValue /></SelectTrigger><SelectContent>{gpuClasses.map((value) => <SelectItem value={value} key={value}>{value.toUpperCase()}</SelectItem>)}</SelectContent></Select></Field>
      </div>
      <Disclosure title="Serving settings"><div className="grid gap-5 border-t border-border px-4 pt-4 @lg:grid-cols-3">
        <Field><FieldLabel htmlFor="maxModelLen">Context limit</FieldLabel><Input id="maxModelLen" name="maxModelLen" type="number" min={64} max={1048576} placeholder="Cluster default" defaultValue={preset?.contextLength} disabled={disabled} /></Field>
        <Field><FieldLabel htmlFor="maxNumSeqs">Concurrent sequences</FieldLabel><Input id="maxNumSeqs" name="maxNumSeqs" type="number" min={1} max={1024} placeholder="Cluster default" defaultValue={preset?.maxNumSeqs} disabled={disabled} /></Field>
        <Field><FieldLabel htmlFor="execution">Execution</FieldLabel><Select name="execution" defaultValue={preset?.execution ?? "default"} items={executionModes} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="execution" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">Cluster default</SelectItem><SelectItem value="eager">Eager</SelectItem><SelectItem value="cuda_graph">CUDA graphs</SelectItem></SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="autoToolChoice">Automatic tool calling</FieldLabel><Select name="autoToolChoice" defaultValue={preset ? preset.toolCalling ? "enabled" : "disabled" : "default"} items={toolChoiceModes} disabled={disabled} onValueChange={onSelectChange}><SelectTrigger id="autoToolChoice" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">Model default</SelectItem><SelectItem value="enabled">Enabled</SelectItem><SelectItem value="disabled">Disabled</SelectItem></SelectContent></Select><FieldDescription>Allow the model to select tools supplied in an API request.</FieldDescription></Field>
        <Field className="@lg:col-span-3"><FieldLabel htmlFor="kernelMode">Kernel mode</FieldLabel><Select name="kernelMode" value={kernelMode} items={kernelModes.map(({ value, label }) => ({ value, label }))} disabled={disabled} onValueChange={(value) => { setKernelMode(String(value)); onSelectChange?.() }}><SelectTrigger id="kernelMode" className="w-full @lg:w-1/3"><SelectValue /></SelectTrigger><SelectContent>{kernelModes.map((mode) => <SelectItem key={mode.value} value={mode.value}>{mode.label}</SelectItem>)}</SelectContent></Select><FieldDescription>{kernelModes.find((mode) => mode.value === kernelMode)?.description}</FieldDescription></Field>
        <Field className="@lg:col-span-2"><FieldLabel htmlFor="toolCallParser">Tool parser</FieldLabel><Input id="toolCallParser" name="toolCallParser" placeholder="Model default" defaultValue={preset?.toolCalling ? preset.toolCallParser : undefined} maxLength={64} pattern="[a-z][a-z0-9_-]*" disabled={disabled} autoComplete="off" /><FieldDescription>Known Qwen models select a compatible parser automatically. Use hermes for Qwen3-VL or qwen3_xml for Qwen3.5. Other models need a parser supported by the model-host image.</FieldDescription></Field>
      </div><p className="px-4 pt-4 pb-4 text-xs text-muted-foreground">The operator selects a supported dtype from the GPU profile. Model memory fit still depends on model size, context, and serving settings.</p></Disclosure>
      <Disclosure title="Routing" section="routing"><div className="grid gap-5 border-t border-border p-4">
        <input type="hidden" name="routing" value="1" />
        <Field><FieldLabel htmlFor="strategy">Balancing strategy</FieldLabel><Select name="strategy" value={strategy} items={balancingStrategies.map(({ value, label }) => ({ value, label }))} disabled={disabled} onValueChange={(value) => { setStrategy(String(value)); onSelectChange?.() }}><SelectTrigger id="strategy" className="w-full @lg:w-1/2"><SelectValue /></SelectTrigger><SelectContent>{balancingStrategies.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent></Select><FieldDescription>How the gateway spreads requests across this deployment’s replicas. {balancingStrategies.find((option) => option.value === strategy)?.description}</FieldDescription></Field>
        <Field><FieldLabel id="capabilities-label">Capabilities</FieldLabel><div role="group" aria-labelledby="capabilities-label" className="grid gap-3 rounded-xl border border-border p-3 @md:grid-cols-2 @lg:grid-cols-4">{capabilityFlags.map((flag) => <label key={flag.value} className="flex items-start gap-2 text-sm" title={flag.description}><Checkbox name="capabilities" value={flag.value} defaultChecked={defaultCapabilities.includes(flag.value)} disabled={disabled} onCheckedChange={onSelectChange} className="mt-0.5" /><span className="min-w-0"><span className="block leading-5">{flag.label}</span><span className="block text-xs text-muted-foreground">{flag.description}</span></span></label>)}</div><FieldDescription>What this model can serve. With <code className="font-mono text-xs">model: &quot;auto&quot;</code>, the gateway picks among the account’s deployments whose capabilities fit the request.</FieldDescription></Field>
        <label className="flex items-start gap-2 text-sm"><Checkbox name="autoEnabled" defaultChecked disabled={disabled} onCheckedChange={onSelectChange} className="mt-0.5" /><span><span className="block leading-5">Eligible for automatic routing</span><span className="block text-xs text-muted-foreground">Clear to serve this deployment only by its API model name.</span></span></label>
        <div className="grid gap-5 @lg:grid-cols-2">
          <Field><FieldLabel htmlFor="priority">Priority</FieldLabel><Input id="priority" name="priority" type="number" min={priorityRange.min} max={priorityRange.max} step={1} defaultValue={0} disabled={disabled} /><FieldDescription>Breaks ties between deployments that fit an automatic request equally well; higher wins. From {priorityRange.min} to {priorityRange.max}.</FieldDescription></Field>
          <Field><FieldLabel htmlFor="cascadeTier">Cascade tier</FieldLabel><Input id="cascadeTier" name="cascadeTier" type="number" min={cascadeTierRange.min} max={cascadeTierRange.max} step={1} placeholder="Not in an explicit ladder" disabled={disabled} /><FieldDescription>Optional. With <code className="font-mono text-xs">model: &quot;cascade&quot;</code>, tiered deployments are tried lowest tier first and escalate when an answer fails its checks. Without any tiers, the ladder follows priority, lowest first.</FieldDescription></Field>
        </div>
      </div></Disclosure>
    </FieldGroup>
  </div>
}

function Chip({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return <span className="inline-flex items-center gap-1.5 rounded-md bg-muted px-2 py-0.5 text-xs text-muted-foreground [&_svg]:size-3.5">{icon}{children}</span>
}

const cardClass = "group flex h-full min-w-0 flex-col gap-3 rounded-2xl border border-border bg-card p-5 text-left outline-none transition-[border-color,background-color,box-shadow] duration-(--motion-fast) hover:border-foreground/25 hover:shadow-[0_2px_8px_rgb(0_0_0/0.04)] focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"

function ModelCardBody({ model }: { model: CatalogModel }) {
  return <>
    <span className="flex items-start justify-between gap-3">
      <IconTile><BoxIcon /></IconTile>
      {model.toolCalling ? <span className="inline-flex items-center gap-1 rounded-full bg-brand-tint px-2 py-0.5 text-xs font-medium text-brand-foreground"><WrenchIcon className="size-3" />Tools</span> : null}
    </span>
    <span className="min-w-0">
      <span className="block text-base font-medium">{model.name}</span>
      <span className="mt-0.5 block text-sm text-muted-foreground">{model.strength}</span>
    </span>
    <span className="mt-auto flex flex-wrap gap-1.5 pt-1"><Chip icon={<BoxIcon />}>{model.size}</Chip><Chip icon={<CpuIcon />}>{model.gpu}</Chip></span>
  </>
}

function CustomCardBody() {
  return <>
    <IconTile tone="neutral"><PlusIcon /></IconTile>
    <span className="min-w-0">
      <span className="block text-base font-medium">Custom model…</span>
      <span className="mt-0.5 block text-sm text-muted-foreground">Any Hugging Face repository; set every field yourself.</span>
    </span>
  </>
}

/**
 * The model picker, step by step: choose a catalog model (or a custom one), then confirm
 * where it runs and what it is called. With `linkBase` the choices are links to
 * `<linkBase>?model=<id>` (the full page, which works without JavaScript); otherwise
 * `onSelect` switches steps in place.
 * The fields carry the same names the create-deployment action has always read.
 */
export function NewDeploymentFlow({ stamps, selection, onSelect, linkBase, disabled = false, onFieldChange }: {
  stamps: DeploymentStampChoice[]
  selection: ModelSelection
  onSelect?: (selection: ModelSelection) => void
  linkBase?: string
  disabled?: boolean
  onFieldChange?: () => void
}) {
  const selectHref = linkBase === undefined ? undefined : (next: ModelSelection) => next ? `${linkBase}?model=${encodeURIComponent(next)}` : linkBase
  const choice = (id: ModelSelection, body: ReactNode, className?: string) => selectHref
    ? <Link key={id ?? "none"} href={selectHref(id)} className={cn(cardClass, className)} aria-label={id === "custom" ? "Custom model" : catalogModel(id)?.name}>{body}</Link>
    : <button key={id ?? "none"} type="button" disabled={disabled} className={cn(cardClass, className)} onClick={() => onSelect?.(id)}>{body}</button>

  if (!selection) {
    return <div className="@container flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">Pick a model to serve. Every setting stays editable before you deploy.</p>
      <div className="grid gap-3 @lg:grid-cols-2">
        {modelCatalog.map((model) => choice(model.id, <ModelCardBody model={model} />))}
        {choice("custom", <CustomCardBody />, "border-dashed bg-muted/30")}
      </div>
    </div>
  }

  if (selection === "custom") {
    return <div className="flex flex-col gap-5">
      <SelectedModel title="Custom model" detail="Any Hugging Face repository the model-host image supports." selectHref={selectHref} onSelect={onSelect} disabled={disabled} />
      <DeploymentFields stamps={stamps} disabled={disabled} onSelectChange={onFieldChange} />
    </div>
  }

  const model = catalogModel(selection)
  if (!model) return null
  return <ConfirmModel key={model.id} model={model} stamps={stamps} selectHref={selectHref} onSelect={onSelect} disabled={disabled} onFieldChange={onFieldChange} />
}

function SelectedModel({ title, detail, chips, selectHref, onSelect, disabled }: { title: string; detail: string; chips?: ReactNode; selectHref?: (selection: ModelSelection) => string; onSelect?: (selection: ModelSelection) => void; disabled: boolean }) {
  return <div className="flex items-start gap-4 rounded-2xl border border-border bg-muted/40 p-4">
    <IconTile><BoxIcon /></IconTile>
    <div className="min-w-0 flex-1">
      <div className="text-base font-medium">{title}</div>
      <div className="mt-0.5 text-sm text-muted-foreground">{detail}</div>
      {chips ? <div className="mt-2.5 flex flex-wrap gap-1.5">{chips}</div> : null}
    </div>
    {selectHref ? <Button variant="outline" size="sm" nativeButton={false} render={<Link href={selectHref(null)} />}>Change</Button> : <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={() => onSelect?.(null)}>Change</Button>}
  </div>
}

function ConfirmModel({ model, stamps, selectHref, onSelect, disabled, onFieldChange }: {
  model: CatalogModel
  stamps: DeploymentStampChoice[]
  selectHref?: (selection: ModelSelection) => string
  onSelect?: (selection: ModelSelection) => void
  disabled: boolean
  onFieldChange?: () => void
}) {
  const fitting = stamps.filter((stamp) => stampFitsClass(stamp, model.gpuClass) !== false)
  const hidden = stamps.length - fitting.length
  const labelFor = (stamp: DeploymentStampChoice) => `${stamp.label.split(" · ")[0]} · ${stamp.gpus ?? "GPUs not reported"}`
  const items = [{ value: "auto", label: "Automatic placement" }, ...fitting.map((stamp) => ({ value: stamp.id, label: labelFor(stamp) }))]
  return <div className="flex flex-col gap-6">
    <SelectedModel title={model.name} detail={model.strength} chips={<><Chip icon={<BoxIcon />}>{model.modelRef}</Chip><Chip icon={<CpuIcon />}>{model.gpu}</Chip>{model.toolCalling ? <Chip icon={<WrenchIcon />}>Tools · {model.toolCallParser}</Chip> : null}</>} selectHref={selectHref} onSelect={onSelect} disabled={disabled} />
    <FieldGroup>
      <Field><FieldLabel htmlFor="stampId">Infrastructure</FieldLabel>
        <Select name="stampId" defaultValue="auto" items={items} disabled={disabled} onValueChange={onFieldChange}>
          <SelectTrigger id="stampId" className="w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="auto">Automatic placement</SelectItem>
            {fitting.map((stamp) => <SelectItem key={stamp.id} value={stamp.id}>{labelFor(stamp)}</SelectItem>)}
          </SelectContent>
        </Select>
        <FieldDescription>{fitting.length ? `Automatic placement picks a connected stamp with a free ${model.gpuClass.toUpperCase()}-class GPU or better.` : "No connected stamp reports a GPU this model fits on. Automatic placement waits until one does."}{hidden ? ` ${hidden} stamp${hidden === 1 ? " is" : "s are"} hidden: ${hidden === 1 ? "its GPUs are" : "their GPUs are"} too small.` : ""}</FieldDescription>
      </Field>
      <Field><FieldLabel htmlFor="name">Deployment name</FieldLabel><Input id="name" name="name" defaultValue={suggestedDeploymentName(model)} pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?" maxLength={200} required disabled={disabled} autoComplete="off" /><FieldDescription>Lowercase letters, numbers and hyphens. Clients call the model as <code className="font-mono text-xs">{model.modelAlias}</code>.</FieldDescription></Field>
    </FieldGroup>
    <Disclosure title="Advanced" section="advanced">
      <div className="border-t border-border p-4">
        <DeploymentFields key={model.id} stamps={stamps} disabled={disabled} onSelectChange={onFieldChange} preset={model} withPlacement={false} />
      </div>
    </Disclosure>
  </div>
}

/**
 * "Create deployment" opens this drawer in place. The trigger is a link to
 * /deployments/new, so modified clicks and visits without JavaScript use the full page.
 * On success the server action redirects to the new deployment.
 */
export function CreateDeploymentDrawer({ stamps, infrastructureUnavailable = false, disabled = false, label = "Create deployment", icon = true }: {
  stamps: DeploymentStampChoice[]
  /** The stamp list could not be read: deploying is blocked, as on the full page. */
  infrastructureUnavailable?: boolean
  disabled?: boolean
  /** The trigger's text, e.g. "Deploy" on the home page's quick-start card. */
  label?: string
  icon?: boolean
}) {
  const drawer = useFormDrawer()
  return <>
    <DrawerTriggerButton trigger={{ label, icon: icon ? PlusIcon : undefined, disabled, href: "/deployments/new" }} triggerRef={drawer.triggerRef} onOpen={() => drawer.setOpen(true)} />
    <CreateDeploymentSession key={drawer.session} open={drawer.open} setOpen={drawer.setOpen} triggerRef={drawer.triggerRef} onOpenChangeComplete={drawer.onOpenChangeComplete} stamps={stamps} blocked={infrastructureUnavailable} />
  </>
}

function CreateDeploymentSession({ open, setOpen, triggerRef, onOpenChangeComplete, stamps, blocked }: {
  open: boolean
  setOpen: (open: boolean) => void
  triggerRef: ReturnType<typeof useFormDrawer>["triggerRef"]
  onOpenChangeComplete: (open: boolean) => void
  stamps: DeploymentStampChoice[]
  blocked: boolean
}) {
  const [state, formAction, pending] = useActionState(createDeployment, {} as ActionState)
  const [selection, setSelection] = useState<ModelSelection>(null)
  const [dirty, setDirty] = useState(false)
  const markDirty = useCallback(() => setDirty(true), [])
  const { request, confirm } = useGuardedClose({ pending, dirty, close: () => setOpen(false) })
  const select = (next: ModelSelection) => { setSelection(next); if (next) markDirty() }

  return <>
    <DrawerPanel open={open} onRequestClose={request} onOpenChangeComplete={onOpenChangeComplete} size="wide" title="Deploy a model" description={selection ? "Confirm where it runs and what it is called." : "Choose a model. Fabric places it on a GPU stamp and serves it."} icon={RocketIcon} finalFocus={triggerRef} closable={!pending}>
      <form action={formAction} onInput={markDirty} onChange={markDirty} className="flex min-h-0 min-w-0 flex-1 flex-col">
        <FormDrawerBody>
          <fieldset disabled={pending} className="m-0 flex min-w-0 flex-col gap-5 border-0 p-0">
            {blocked ? <Alert variant="destructive"><RocketIcon /><AlertTitle>Infrastructure could not be checked</AlertTitle><AlertDescription>Refresh before deploying. No capacity has been assumed available.</AlertDescription></Alert> : null}
            <NewDeploymentFlow stamps={stamps} selection={selection} onSelect={select} disabled={pending} onFieldChange={markDirty} />
          </fieldset>
        </FormDrawerBody>
        <FormDrawerFooter error={state.error}>
          {selection ? <Button type="button" variant="ghost" className="mr-auto" disabled={pending} onClick={() => setSelection(null)}><ArrowLeftIcon />All models</Button> : null}
          <Button type="button" variant="outline" disabled={pending} onClick={() => request()}>Cancel</Button>
          {selection ? <DeploySubmit disabled={blocked} className="min-w-36" /> : null}
        </FormDrawerFooter>
      </form>
    </DrawerPanel>
    {confirm}
  </>
}
