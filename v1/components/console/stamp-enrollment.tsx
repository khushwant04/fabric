"use client"

import { useActionState, useId, useState, type FormEvent, type RefObject } from "react"
import { ArrowLeftIcon, ArrowRightIcon, CheckIcon, ChevronRightIcon, CopyIcon, ExternalLinkIcon, KeyRoundIcon, Maximize2Icon, Minimize2Icon, PlusIcon, ServerIcon, Settings2Icon, ShieldCheckIcon, TerminalIcon } from "lucide-react"

import { createEnrollmentToken, type ActionState } from "@/app/(console)/actions"
import { DrawerPanel, DrawerTriggerButton, FormDrawerBody, FormDrawerFooter, useFormDrawer, useGuardedClose } from "@/components/console/form-drawer"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"
import {
  STAMP_BOOTSTRAP_URL,
  STAMP_MODEL_IMAGE,
  STAMP_RELEASE,
  STAMP_REPOSITORY,
  stampBootstrapCommand,
  stampHelmCommand,
  stampSetupError,
  type StampEnrollmentConfig,
  type StampSetup,
} from "@/lib/fabric/stamp-enrollment"

type SetupPanel = "cluster" | "enrollment" | "runtime"

const panels = [
  { id: "cluster", title: "Prepare cluster", description: "VM setup or existing Kubernetes", icon: ServerIcon },
  { id: "enrollment", title: "Enroll stamp", description: "Connect the Fabric operator", icon: KeyRoundIcon },
  { id: "runtime", title: "Runtime & gateway", description: "GPU runtime, hostname and TLS", icon: Settings2Icon },
] as const

function CopyCommand({ command, label }: { command: string; label: string }) {
  const [copied, setCopied] = useState("")
  const [error, setError] = useState("")
  return <div className="w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-border bg-muted/30">
    <div className="flex min-w-0 items-center justify-between gap-3 border-b border-border/50 px-3 py-2">
      <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground"><TerminalIcon className="size-3.5 shrink-0" /><span className="truncate">{label}</span></span>
      <Button type="button" variant="ghost" size="sm" aria-label={`Copy ${label}`} onClick={async () => {
        try { await navigator.clipboard.writeText(command); setCopied(command); setError("") }
        catch { setError("Copy is unavailable. Select the command below and copy it manually.") }
      }}>{copied === command ? <CheckIcon /> : <CopyIcon />}{copied === command ? "Copied" : "Copy"}</Button>
    </div>
    <pre tabIndex={0} aria-label={label} className="max-h-80 w-full min-w-0 max-w-full overflow-auto p-4 font-mono text-[11px] leading-6 whitespace-pre-wrap break-all"><code>{command}</code></pre>
    {error ? <p role="status" className="px-4 pb-3 text-xs text-destructive">{error}</p> : null}
  </div>
}

// Enrollment opens in a wide drawer with three steps. The single-use token lives only in
// the drawer session's action state, which is discarded when the drawer closes.
export function EnrollmentTokenDrawer({ disabled = false, config }: { disabled?: boolean; config: StampEnrollmentConfig }) {
  const drawer = useFormDrawer()
  return <>
    <DrawerTriggerButton trigger={{ label: "Enroll stamp", icon: PlusIcon, disabled }} triggerRef={drawer.triggerRef} onOpen={() => drawer.setOpen(true)} />
    <EnrollmentSession key={drawer.session} config={config} open={drawer.open} setOpen={drawer.setOpen} triggerRef={drawer.triggerRef} onOpenChangeComplete={drawer.onOpenChangeComplete} />
  </>
}

function EnrollmentSession({ config, open, setOpen, triggerRef, onOpenChangeComplete }: {
  config: StampEnrollmentConfig
  open: boolean
  setOpen: (open: boolean) => void
  triggerRef: RefObject<HTMLElement | null>
  onOpenChangeComplete: (open: boolean) => void
}) {
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [panel, setPanel] = useState<SetupPanel>("cluster")
  const [state, action, pending] = useActionState<ActionState, FormData>(createEnrollmentToken, {})
  const [expiresInMinutes, setExpiresInMinutes] = useState("60")
  const [nvidia, setNvidia] = useState(true)
  const [setup, setSetup] = useState<StampSetup>({
    target: "k3s", stampName: "gpu-stamp", controlPlaneUrl: config.controlPlaneUrl,
    jwtIssuer: config.jwtIssuer, kubeContext: "", runtimeClass: "nvidia",
    modelImage: STAMP_MODEL_IMAGE, gatewayHost: "", ingressClass: "traefik", certificateIssuer: "", acmeEmail: "",
  })
  const update = (key: keyof StampSetup, value: string) => { setDirty(true); setSetup((previous) => ({ ...previous, [key]: value })) }
  const effectiveSetup = { ...setup, jwtIssuer: state.enrollmentIssuer || setup.jwtIssuer }
  const setupError = stampSetupError(effectiveSetup, false)
  const expiry = Number(expiresInMinutes)
  const expiryError = Number.isInteger(expiry) && expiry >= 5 && expiry <= 1440 ? null : "Choose a token lifetime between 5 minutes and 24 hours."
  const generationError = setupError || expiryError
  const k3s = setup.target === "k3s"
  const command = !state.enrollmentToken ? null
    : k3s ? stampBootstrapCommand(effectiveSetup, state.enrollmentToken, nvidia)
      : stampHelmCommand(effectiveSetup, state.enrollmentToken)
  const commandError = state.enrollmentToken ? stampSetupError(effectiveSetup) : null
  const selectedPanel = panels.find((item) => item.id === panel)!
  const PanelIcon = selectedPanel.icon
  // A generated token is lost on close (it is single-use and never stored), so closing
  // then asks for confirmation, as it does for edited settings.
  const { request, confirm } = useGuardedClose({
    pending,
    dirty: dirty || !!state.enrollmentToken,
    close: () => setOpen(false),
    copy: state.enrollmentToken
      ? { title: "Close enrollment?", description: "The enrollment command and its single-use token are cleared when this drawer closes. Copy the command first if you still need it." }
      : { title: "Discard stamp setup?", description: "The settings you entered for this stamp will be lost." },
  })

  // A newly issued token is shown on the enrollment panel. Adjusting state during render
  // (rather than in an effect) avoids rendering the previous panel once more first.
  const [shownToken, setShownToken] = useState(state.enrollmentToken)
  if (state.enrollmentToken !== shownToken) {
    setShownToken(state.enrollmentToken)
    if (state.enrollmentToken) setPanel("enrollment")
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    if (pending || state.enrollmentToken || generationError || panel !== "enrollment") event.preventDefault()
  }

  return <>
    <DrawerPanel open={open} onRequestClose={request} onOpenChangeComplete={onOpenChangeComplete} size={expanded ? "xl" : "wide"} title="Enroll an inference stamp" description="Connect your infrastructure to Fabric." icon={ServerIcon} finalFocus={triggerRef} closable={!pending}>
      <form action={state.enrollmentToken ? undefined : action} onSubmit={handleSubmit} className="flex min-h-0 min-w-0 flex-1 flex-col">
        <input type="hidden" name="mode" value="byoi" />
        <input type="hidden" name="expiresInMinutes" value={expiresInMinutes} />
        <nav aria-label="Stamp setup steps" className="shrink-0 border-b border-border/60 px-3 py-2 sm:px-4">
          <ol className="grid grid-cols-3 gap-1">
            {panels.map((item, index) => {
              const done = item.id === "cluster" ? panel !== "cluster" : item.id === "enrollment" ? !!state.enrollmentToken : false
              return <li key={item.id} className="min-w-0">
                <button type="button" disabled={pending} aria-current={panel === item.id ? "step" : undefined} onClick={() => setPanel(item.id)} className={cn(
                  "flex w-full min-w-0 items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors outline-none hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 sm:px-3",
                  panel === item.id ? "bg-muted text-foreground" : "text-muted-foreground",
                )}>
                  <span aria-hidden="true" className={cn("hidden size-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-medium tabular-nums sm:flex", panel === item.id ? "border-foreground/20 bg-background text-foreground" : "border-border")}>{done ? <CheckIcon className="size-3.5" /> : index + 1}</span>
                  <span className="min-w-0 flex-1"><span className="block truncate text-xs font-medium sm:text-[13px]">{item.title}</span><span className="mt-0.5 hidden truncate text-[11px] font-normal text-muted-foreground md:block">{item.description}</span></span>
                </button>
              </li>
            })}
          </ol>
        </nav>

        <FormDrawerBody key={panel} role="region" aria-labelledby={`${id}-panel-title`} className="space-y-5 [overflow-wrap:anywhere] [&_[data-slot=field]]:min-w-0">
          <div className="flex min-w-0 items-center gap-2.5">
            <PanelIcon className="size-4 shrink-0 text-muted-foreground" /><h3 id={`${id}-panel-title`} className="text-sm font-medium">{selectedPanel.title}</h3>
            {panel === "enrollment" ? <a href={k3s ? STAMP_BOOTSTRAP_URL : `${STAMP_REPOSITORY}/tree/${STAMP_RELEASE}/deploy/helm/fabric-stamp`} target="_blank" rel="noreferrer" className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">{k3s ? "Setup script" : "Helm chart"}<ExternalLinkIcon className="size-3" /></a> : null}
          </div>

          {panel === "cluster" ? <>
            <p className="text-sm leading-6 text-muted-foreground">Create a k3s cluster on your VM, or connect a Kubernetes cluster you already run.</p>
            <div className="grid min-w-0 gap-4 sm:grid-cols-2">
              <Field><FieldLabel htmlFor={`${id}-target`}>Cluster</FieldLabel><Select value={setup.target} items={[{ value: "k3s", label: "Create k3s on a VM" }, { value: "kubernetes", label: "Existing Kubernetes" }]} disabled={pending} onValueChange={(value) => {
                if (value !== "k3s" && value !== "kubernetes") return
                setDirty(true)
                setSetup((previous) => ({ ...previous, target: value, runtimeClass: value === "k3s" ? (nvidia ? "nvidia" : "") : "", ingressClass: value === "k3s" ? "traefik" : "" }))
              }}><SelectTrigger id={`${id}-target`} className="w-full min-w-0"><SelectValue className="min-w-0 truncate" /></SelectTrigger><SelectContent><SelectItem value="k3s">Create k3s on a VM</SelectItem><SelectItem value="kubernetes">Existing Kubernetes</SelectItem></SelectContent></Select></Field>
              <Field><FieldLabel htmlFor={`${id}-name`}>Stamp name</FieldLabel><Input id={`${id}-name`} value={setup.stampName} onChange={(event) => update("stampName", event.target.value)} maxLength={53} disabled={pending} /></Field>
            </div>
            {setup.target === "k3s" ? <>
              <div className="space-y-3">
                <h4 className="text-sm font-medium">One command sets up the whole VM</h4>
                <p className="text-xs leading-5 text-muted-foreground">After you generate a token, you get a single command. It installs k3s with GPU support, TLS for the public hostname and the Fabric stamp, then waits until the stamp has enrolled. Re-running it later upgrades or repairs the stamp without a new token.</p>
                <label className="flex items-center gap-2 text-xs"><Checkbox checked={nvidia} disabled={pending} onCheckedChange={(checked) => { setNvidia(checked === true); update("runtimeClass", checked === true ? "nvidia" : "") }} />This VM has NVIDIA GPUs or Cloud TPU chips</label>
              </div>
              <div className="overflow-hidden rounded-xl border border-border">
                <p className="border-b border-border/60 px-3 py-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Before you run it</p>
                <ul className="divide-y divide-border/60 text-xs">
                  <li className="px-3 py-2">Ubuntu 22.04 or 24.04 on x86_64, with systemd.</li>
                  {nvidia ? <li className="px-3 py-2">The NVIDIA driver is installed and <code>nvidia-smi</code> works. vLLM 0.26 needs driver R580 or newer. On a single-host Cloud TPU VM (for example v6e-4) no NVIDIA driver is needed: the command detects the TPU and serves models with the vLLM TPU image.</li> : null}
                  <li className="px-3 py-2">For public inference: a DNS A record for the stamp hostname pointing at the VM, and inbound ports 80 and 443 open.</li>
                  <li className="px-3 py-2">Outbound HTTPS to the control plane, GitHub, the image registry and Hugging Face.</li>
                </ul>
              </div>
            </> : <Field><FieldLabel htmlFor={`${id}-context`}>Kubectl context</FieldLabel><Input id={`${id}-context`} value={setup.kubeContext} onChange={(event) => update("kubeContext", event.target.value)} placeholder="Context from kubectl config get-contexts" disabled={pending} /><FieldDescription>Run Helm on a machine with this context, cluster-admin access and working GPU support.</FieldDescription></Field>}
          </> : null}

          {panel === "enrollment" ? <>
            <p className="text-sm leading-6 text-muted-foreground">{k3s ? "Run one command on the VM. It prepares the machine, installs the stamp and joins it to this account." : "Install the Fabric Helm chart on your cluster. Its operator joins this account and reconciles the models you deploy."}</p>
            <Field><FieldLabel htmlFor={`${id}-control`}>Control-plane address</FieldLabel><Input id={`${id}-control`} type="url" value={setup.controlPlaneUrl} onChange={(event) => update("controlPlaneUrl", event.target.value)} placeholder="https://control.your-domain.com" disabled={pending} /><FieldDescription>The HTTPS address reachable from your cluster.</FieldDescription></Field>
            <div className="rounded-xl border border-border px-4 py-3 text-xs"><p className="font-medium">Release</p><p className="mt-1 font-mono text-[11px] text-muted-foreground">{STAMP_RELEASE.slice(0, 12)} · chart, agent, gateway and model host</p></div>
            {k3s ? <p className="text-xs leading-5 text-muted-foreground">{setup.gatewayHost ? <>Public endpoint: <code>https://{setup.gatewayHost}</code> with a Let&apos;s Encrypt certificate.</> : <>No public hostname yet: the stamp enrolls, but the edge gateway cannot route to it. Add one in Runtime &amp; gateway.</>}</p> : null}
            {state.enrollmentToken ? <>
              <Alert><KeyRoundIcon /><AlertTitle>{k3s ? "Setup command ready" : "Enrollment command ready"}</AlertTitle><AlertDescription>Includes your single-use token, valid until {state.expiresAt ? new Date(state.expiresAt).toLocaleString() : "its expiry"}. Closing this drawer clears it.</AlertDescription></Alert>
              {commandError ? <FieldError>{commandError}</FieldError> : null}
              {command ? <CopyCommand command={command} label={k3s ? "Run on the VM" : "Helm enrollment command"} /> : null}
              <p className="text-xs leading-5 text-muted-foreground">{k3s ? "Takes a few minutes and finishes when the stamp has enrolled. The first model you deploy also pulls the model-host image once." : "Run once for a new stamp. Helm stores the token in its release and enrollment Secret; the agent retains its identity on persistent storage."}</p>
              {setup.gatewayHost ? <p className="text-xs leading-5 text-muted-foreground">Ready placements report <code>https://{setup.gatewayHost}</code>. Approve this origin in the console gateway configuration so the console can reach it.</p> : <p className="text-xs leading-5 text-muted-foreground">Public inference also needs a gateway hostname and TLS. You can configure these in Runtime &amp; gateway.</p>}
            </> : <>
              <Field><FieldLabel htmlFor={`${id}-expiry`}>Token lifetime (minutes)</FieldLabel><Input id={`${id}-expiry`} type="number" min={5} max={1440} value={expiresInMinutes} onChange={(event) => { setDirty(true); setExpiresInMinutes(event.target.value) }} disabled={pending} /><FieldDescription>Generate after your cluster is ready. Valid for 5 minutes to 24 hours.</FieldDescription></Field>
              <div className="rounded-xl bg-muted/40 px-4 py-3"><p className="flex items-center gap-2 text-xs font-medium"><TerminalIcon className="size-3.5 text-muted-foreground" />{k3s ? "Your setup command will appear here" : "Your Helm command will appear here"}</p><p className="mt-2 text-xs leading-5 text-muted-foreground">Generate a token to fill the command with your stamp name, cluster settings and account credential.</p></div>
              {generationError ? <FieldError>{generationError}</FieldError> : null}
            </>}
            {state.error ? <FieldError>{state.error}</FieldError> : null}
          </> : null}

          {panel === "runtime" && k3s ? <>
            <p className="text-sm leading-6 text-muted-foreground">Give the stamp a public HTTPS hostname so the edge gateway can route requests to it. The setup command issues its certificate.</p>
            <Field><FieldLabel htmlFor={`${id}-gateway`}>Stamp hostname</FieldLabel><Input id={`${id}-gateway`} value={setup.gatewayHost} onChange={(event) => update("gatewayHost", event.target.value)} placeholder="gpu-01.stamps.your-domain.com" disabled={pending} /><FieldDescription>Create a DNS A record pointing at the VM&apos;s public IP first. Leave empty to enroll without public inference.</FieldDescription></Field>
            {setup.gatewayHost ? <Field><FieldLabel htmlFor={`${id}-email`}>Certificate contact email</FieldLabel><Input id={`${id}-email`} type="email" value={setup.acmeEmail} onChange={(event) => update("acmeEmail", event.target.value)} placeholder="ops@your-domain.com" disabled={pending} /><FieldDescription>Let&apos;s Encrypt sends expiry notices here.</FieldDescription></Field> : null}
            <Field><FieldLabel htmlFor={`${id}-issuer`}>Control-plane JWT issuer</FieldLabel><Input id={`${id}-issuer`} value={effectiveSetup.jwtIssuer} onChange={(event) => update("jwtIssuer", event.target.value)} placeholder="Detected when the token is generated" disabled={pending || !!state.enrollmentIssuer} /></Field>
            <p className="text-xs leading-5 text-muted-foreground">GPU runtime, model dtype (bf16 on Ampere and newer, fp16 on T4) and the model-host image are chosen by the setup command.</p>
          </> : null}

          {panel === "runtime" && !k3s ? <>
            <p className="text-sm leading-6 text-muted-foreground">Choose the model runtime and optional public inference gateway for this cluster.</p>
            <Field><FieldLabel htmlFor={`${id}-image`}>vLLM image</FieldLabel><Input id={`${id}-image`} value={setup.modelImage} onChange={(event) => update("modelImage", event.target.value)} disabled={pending} /><FieldDescription>Standard vLLM 0.26 uses CUDA 13 and needs an R580 or newer driver. A10 GRID 570 / CUDA 12.8 can use <code>vllm/vllm-openai:v0.11.0</code> for supported models. Qwen3.5 needs a newer compatible runtime.</FieldDescription></Field>
            <div className="grid min-w-0 gap-4 sm:grid-cols-2">
              <Field><FieldLabel htmlFor={`${id}-runtime`}>GPU RuntimeClass</FieldLabel><Input id={`${id}-runtime`} value={setup.runtimeClass} onChange={(event) => update("runtimeClass", event.target.value)} placeholder="Default runtime if empty" disabled={pending} /></Field>
              <Field><FieldLabel htmlFor={`${id}-issuer`}>Control-plane JWT issuer</FieldLabel><Input id={`${id}-issuer`} value={effectiveSetup.jwtIssuer} onChange={(event) => update("jwtIssuer", event.target.value)} placeholder="Detected when the token is generated" disabled={pending || !!state.enrollmentIssuer} /></Field>
            </div>
            <div className="space-y-4 border-t border-border/40 pt-5"><h4 className="text-sm font-medium">Inference gateway</h4>
              <Field><FieldLabel htmlFor={`${id}-gateway`}>Hostname (optional)</FieldLabel><Input id={`${id}-gateway`} value={setup.gatewayHost} onChange={(event) => update("gatewayHost", event.target.value)} placeholder="inference.your-domain.com" disabled={pending} /><FieldDescription>Point DNS at your ingress controller. Leave empty to enroll first and set up public inference later.</FieldDescription></Field>
              {setup.gatewayHost ? <div className="grid min-w-0 gap-4 sm:grid-cols-2">
                <Field><FieldLabel htmlFor={`${id}-ingress`}>IngressClass</FieldLabel><Input id={`${id}-ingress`} value={setup.ingressClass} onChange={(event) => update("ingressClass", event.target.value)} disabled={pending} /></Field>
                <Field><FieldLabel htmlFor={`${id}-certificate`}>Existing ClusterIssuer</FieldLabel><Input id={`${id}-certificate`} value={setup.certificateIssuer} onChange={(event) => update("certificateIssuer", event.target.value)} placeholder="cert-manager ClusterIssuer" disabled={pending} /></Field>
              </div> : null}
            </div>
          </> : null}

          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/40 pt-4 text-[11px] text-muted-foreground">
            <span className="truncate font-medium text-foreground">{setup.stampName || "Your stamp"}</span>
            <span>{setup.target === "k3s" ? "k3s on your VM" : "Existing Kubernetes"}</span>
            <span className="inline-flex items-center gap-1"><ShieldCheckIcon className="size-3.5" />{state.enrollmentToken ? "Single-use token generated" : "Account-scoped enrollment"}</span>
            <span className="basis-full leading-5">Install the operator once. Deploy and manage models from the dashboard.</span>
          </div>
        </FormDrawerBody>

        <FormDrawerFooter>
          <div className="mr-auto flex min-w-0 items-center gap-2">
            <Button type="button" variant="ghost" size="icon-sm" className="hidden md:inline-flex" aria-label={expanded ? "Restore drawer width" : "Widen drawer"} aria-pressed={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? <Minimize2Icon /> : <Maximize2Icon />}</Button>
            <span className="hidden truncate text-[11px] text-muted-foreground sm:block">{panel === "cluster" ? "Prepare your infrastructure" : panel === "runtime" ? (k3s ? "Settings apply to the setup command" : "Settings apply to the Helm command") : state.enrollmentToken ? "Keep your enrollment token private" : "Enroll when your cluster is ready"}</span>
          </div>
          <Button type="button" variant="outline" disabled={pending} onClick={() => request()}>Close</Button>
          {/* Distinct keys keep each step button its own DOM node: reusing one node would let
              the click that switches to the enrollment step also submit the form. */}
          {panel === "cluster" ? <Button key="continue" type="button" disabled={pending} onClick={() => setPanel("enrollment")}>Continue<ArrowRightIcon /></Button> : panel === "runtime" ? <Button key="back" type="button" disabled={pending} onClick={() => setPanel("enrollment")}><ArrowLeftIcon />Enrollment</Button> : state.enrollmentToken ? <Button key="settings" type="button" disabled={pending} onClick={() => setPanel("runtime")}>Settings<ChevronRightIcon /></Button> : <Button key="generate" type="submit" disabled={pending || !!generationError}>{pending ? <Spinner /> : <KeyRoundIcon />}{pending ? "Generating…" : "Generate token"}</Button>}
        </FormDrawerFooter>
      </form>
    </DrawerPanel>
    {confirm}
  </>
}
