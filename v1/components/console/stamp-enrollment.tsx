"use client"

import { useActionState, useEffect, useId, useState, type FormEvent } from "react"
import { ArrowLeftIcon, ArrowRightIcon, CheckIcon, ChevronRightIcon, CopyIcon, ExternalLinkIcon, KeyRoundIcon, Maximize2Icon, Minimize2Icon, PlusIcon, ServerIcon, Settings2Icon, ShieldCheckIcon, TerminalIcon } from "lucide-react"

import { createEnrollmentToken, type ActionState } from "@/app/(console)/actions"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"
import {
  K3S_BOOTSTRAP_URL,
  STAMP_CHART_REF,
  STAMP_REPOSITORY,
  k3sBootstrapCommand,
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
  return <div className="w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-border/60 bg-muted/30">
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

export function EnrollmentTokenDialog({ disabled = false, config }: { disabled?: boolean; config: StampEnrollmentConfig }) {
  const [session, setSession] = useState(0)
  return <EnrollmentSession key={session} disabled={disabled} config={config} onFinish={() => setSession((value) => value + 1)} />
}

function EnrollmentSession({ disabled, config, onFinish }: { disabled: boolean; config: StampEnrollmentConfig; onFinish: () => void }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [panel, setPanel] = useState<SetupPanel>("cluster")
  const [state, action, pending] = useActionState<ActionState, FormData>(createEnrollmentToken, {})
  const [expiresInMinutes, setExpiresInMinutes] = useState("60")
  const [nvidia, setNvidia] = useState(true)
  const [setup, setSetup] = useState<StampSetup>({
    target: "k3s", stampName: "gpu-stamp", controlPlaneUrl: config.controlPlaneUrl,
    jwtIssuer: config.jwtIssuer, kubeContext: "", runtimeClass: "nvidia",
    modelImage: "vllm/vllm-openai:v0.26.0", gatewayHost: "", ingressClass: "traefik", certificateIssuer: "",
  })
  const update = (key: keyof StampSetup, value: string) => setSetup((previous) => ({ ...previous, [key]: value }))
  const effectiveSetup = { ...setup, jwtIssuer: state.enrollmentIssuer || setup.jwtIssuer }
  const setupError = stampSetupError(effectiveSetup, false)
  const expiry = Number(expiresInMinutes)
  const expiryError = Number.isInteger(expiry) && expiry >= 5 && expiry <= 1440 ? null : "Choose a token lifetime between 5 minutes and 24 hours."
  const generationError = setupError || expiryError
  const bootstrap = k3sBootstrapCommand(setup.stampName, nvidia)
  const helm = state.enrollmentToken ? stampHelmCommand(effectiveSetup, state.enrollmentToken) : null
  const commandError = state.enrollmentToken ? stampSetupError(effectiveSetup) : null
  const selectedPanel = panels.find((item) => item.id === panel)!
  const PanelIcon = selectedPanel.icon

  useEffect(() => { if (state.enrollmentToken) setPanel("enrollment") }, [state.enrollmentToken])

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    if (pending || state.enrollmentToken || generationError || panel !== "enrollment") event.preventDefault()
  }

  return <Dialog open={open} onOpenChange={(next) => {
    if (pending) return
    setOpen(next)
    if (!next) onFinish()
  }}>
    <DialogTrigger render={<Button disabled={disabled}><PlusIcon /> Enroll stamp</Button>} />
    <DialogContent
      className="flex h-[min(720px,calc(100dvh-2rem))] w-[calc(100%-2rem)] min-w-0 flex-col gap-0 overflow-hidden rounded-[24px] border-border/50 bg-background p-0 sm:max-w-[1040px]"
      style={expanded ? { height: "calc(100dvh - 2rem)", maxWidth: "min(1280px, calc(100% - 2rem))" } : undefined}
      showCloseButton={!pending}
    >
      <DialogHeader className="flex min-w-0 shrink-0 flex-row items-center gap-3 border-b border-border/40 px-5 py-4 pr-14">
        <ServerIcon className="size-5 shrink-0 text-muted-foreground" />
        <div className="min-w-0"><DialogTitle className="text-sm">Enroll an inference stamp</DialogTitle><DialogDescription className="mt-1 text-xs">Connect your infrastructure to Fabric.</DialogDescription></div>
      </DialogHeader>
      <form action={state.enrollmentToken ? undefined : action} onSubmit={handleSubmit} className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <input type="hidden" name="mode" value="byoi" />
        <input type="hidden" name="expiresInMinutes" value={expiresInMinutes} />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col md:flex-row">
          <aside className="flex min-h-0 min-w-0 shrink-0 flex-col overflow-y-auto border-b border-border/40 md:w-72 md:border-r md:border-b-0">
            <div className="hidden items-center justify-between px-5 pt-5 pb-3 md:flex"><span className="text-xs text-muted-foreground">Setup</span><span className="text-[11px] text-muted-foreground">3 steps</span></div>
            <nav aria-label="Stamp setup steps" className="flex min-w-0 gap-1 p-2 md:flex-col md:px-3">
              {panels.map((item, index) => <button key={item.id} type="button" disabled={pending} aria-current={panel === item.id ? "step" : undefined} onClick={() => setPanel(item.id)} className={cn(
                "flex min-w-0 flex-1 items-center gap-2 rounded-xl px-2 py-3 text-left transition-colors outline-none hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 md:flex-none md:gap-3 md:px-3",
                panel === item.id ? "bg-muted text-foreground" : "text-muted-foreground",
              )}>
                <item.icon className="hidden size-4 shrink-0 sm:block" />
                <span className="min-w-0 flex-1"><span className="block text-[11px] font-medium sm:text-xs md:text-[13px]">{item.title}</span><span className="mt-1 hidden text-[11px] font-normal text-muted-foreground md:block">{item.description}</span></span>
                <span className="hidden text-[11px] text-muted-foreground md:block">{index + 1}</span>
              </button>)}
            </nav>
            <div className="mt-auto hidden min-w-0 space-y-3 p-5 md:block">
              <div className="rounded-xl border border-border/50 p-3"><p className="truncate text-xs font-medium">{setup.stampName || "Your stamp"}</p><p className="mt-1 text-[11px] text-muted-foreground">{setup.target === "k3s" ? "k3s on your VM" : "Existing Kubernetes"}</p><div className="mt-3 flex items-center gap-1.5 text-[11px] text-muted-foreground"><ShieldCheckIcon className="size-3.5" />{state.enrollmentToken ? "Single-use token generated" : "Account-scoped enrollment"}</div></div>
              <p className="text-[11px] leading-5 text-muted-foreground">Install the operator once. Deploy and manage models from the dashboard.</p>
            </div>
          </aside>

          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="flex min-w-0 shrink-0 items-center gap-3 border-b border-border/40 px-5 py-4 sm:px-6">
              <PanelIcon className="size-4 shrink-0 text-muted-foreground" /><h3 id={`${id}-panel-title`} className="text-sm font-medium">{selectedPanel.title}</h3>
              {panel === "enrollment" ? <a href={`${STAMP_REPOSITORY}/tree/${STAMP_CHART_REF}/deploy/helm/fabric-stamp`} target="_blank" rel="noreferrer" className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">Helm chart<ExternalLinkIcon className="size-3" /></a> : null}
            </div>
            <div key={panel} role="region" aria-labelledby={`${id}-panel-title`} className="min-h-0 min-w-0 flex-1 space-y-5 overflow-x-hidden overflow-y-auto overscroll-contain px-5 py-5 [overflow-wrap:anywhere] sm:px-6 [&_[data-slot=field]]:min-w-0">
              {panel === "cluster" ? <>
                <p className="text-sm leading-6 text-muted-foreground">Create a k3s cluster on your VM, or connect a Kubernetes cluster you already run.</p>
                <div className="grid min-w-0 gap-4 sm:grid-cols-2">
                  <Field><FieldLabel htmlFor={`${id}-target`}>Cluster</FieldLabel><Select value={setup.target} disabled={pending} onValueChange={(value) => {
                    if (value !== "k3s" && value !== "kubernetes") return
                    setSetup((previous) => ({ ...previous, target: value, runtimeClass: value === "k3s" ? (nvidia ? "nvidia" : "") : "", ingressClass: value === "k3s" ? "traefik" : "" }))
                  }}><SelectTrigger id={`${id}-target`} className="w-full min-w-0"><SelectValue className="min-w-0 truncate" /></SelectTrigger><SelectContent><SelectItem value="k3s">Create k3s on a VM</SelectItem><SelectItem value="kubernetes">Existing Kubernetes</SelectItem></SelectContent></Select></Field>
                  <Field><FieldLabel htmlFor={`${id}-name`}>Stamp name</FieldLabel><Input id={`${id}-name`} value={setup.stampName} onChange={(event) => update("stampName", event.target.value)} maxLength={53} disabled={pending} /></Field>
                </div>
                {setup.target === "k3s" ? <>
                  <div className="space-y-3"><h4 className="text-sm font-medium">Create a k3s cluster on your VM</h4><p className="text-xs leading-5 text-muted-foreground">Run this on Ubuntu 22.04 or 24.04. The script installs k3s and Helm, with Traefik and local storage. Existing local k3s installations are reused.</p>
                    <label className="flex items-center gap-2 text-xs"><Checkbox checked={nvidia} disabled={pending} onCheckedChange={(checked) => { setNvidia(checked === true); update("runtimeClass", checked === true ? "nvidia" : "") }} />Enable NVIDIA GPU support</label>
                    {nvidia ? <p className="text-xs leading-5 text-muted-foreground">Install the correct NVIDIA driver first; <code>nvidia-smi</code> must work. This adds container support and measured GPU labels.</p> : null}
                  </div>
                  {bootstrap ? <CopyCommand command={bootstrap} label="VM setup command" /> : <FieldError>Enter a valid stamp name to see the setup command.</FieldError>}
                  <a href={K3S_BOOTSTRAP_URL} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">View the GitHub script<ExternalLinkIcon className="size-3" /></a>
                </> : <Field><FieldLabel htmlFor={`${id}-context`}>Kubectl context</FieldLabel><Input id={`${id}-context`} value={setup.kubeContext} onChange={(event) => update("kubeContext", event.target.value)} placeholder="Context from kubectl config get-contexts" disabled={pending} /><FieldDescription>Run Helm on a machine with this context, cluster-admin access and working GPU support.</FieldDescription></Field>}
              </> : null}

              {panel === "enrollment" ? <>
                <p className="text-sm leading-6 text-muted-foreground">Install the Fabric Helm chart on your cluster. Its operator joins this account and reconciles the models you deploy.</p>
                <Field><FieldLabel htmlFor={`${id}-control`}>Control-plane address</FieldLabel><Input id={`${id}-control`} type="url" value={setup.controlPlaneUrl} onChange={(event) => update("controlPlaneUrl", event.target.value)} placeholder="https://control.your-domain.com" disabled={pending} /><FieldDescription>The HTTPS address reachable from your cluster.</FieldDescription></Field>
                <div className="rounded-xl border border-border/50 px-4 py-3 text-xs"><p className="font-medium">Helm chart</p><p className="mt-1 font-mono text-[11px] text-muted-foreground">deploy/helm/fabric-stamp</p></div>
                <div className="space-y-2 text-xs text-muted-foreground"><p>Model runtime: <code>{setup.modelImage}</code></p><p className="leading-5">vLLM 0.26 needs an R580 or newer driver. For A10 GRID 570, choose a CUDA 12.8 runtime in Runtime &amp; gateway.</p></div>
                {state.enrollmentToken ? <>
                  <Alert><KeyRoundIcon /><AlertTitle>Enrollment command ready</AlertTitle><AlertDescription>Includes your single-use token, valid until {state.expiresAt ? new Date(state.expiresAt).toLocaleString() : "its expiry"}. Closing this dialog clears it.</AlertDescription></Alert>
                  {commandError ? <FieldError>{commandError}</FieldError> : null}
                  {helm ? <CopyCommand command={helm} label="Helm enrollment command" /> : null}
                  <p className="text-xs leading-5 text-muted-foreground">Run once for a new stamp. Helm stores the token in its release and enrollment Secret; the agent retains its identity on persistent storage.</p>
                  {setup.gatewayHost ? <p className="text-xs leading-5 text-muted-foreground">Ready placements report <code>https://{setup.gatewayHost}</code>. Approve this origin in the console gateway configuration for playground access.</p> : <p className="text-xs leading-5 text-muted-foreground">Public inference also needs a gateway hostname and TLS. You can configure these in Runtime &amp; gateway.</p>}
                </> : <>
                  <Field><FieldLabel htmlFor={`${id}-expiry`}>Token lifetime (minutes)</FieldLabel><Input id={`${id}-expiry`} type="number" min={5} max={1440} value={expiresInMinutes} onChange={(event) => setExpiresInMinutes(event.target.value)} disabled={pending} /><FieldDescription>Generate after your cluster is ready. Valid for 5 minutes to 24 hours.</FieldDescription></Field>
                  <div className="rounded-xl bg-muted/40 px-4 py-3"><p className="flex items-center gap-2 text-xs font-medium"><TerminalIcon className="size-3.5 text-muted-foreground" />Your Helm command will appear here</p><p className="mt-2 text-xs leading-5 text-muted-foreground">Generate a token to fill the command with your stamp name, cluster settings and account credential.</p></div>
                  {generationError ? <FieldError>{generationError}</FieldError> : null}
                </>}
                {state.error ? <FieldError>{state.error}</FieldError> : null}
              </> : null}

              {panel === "runtime" ? <>
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
            </div>
          </div>
        </div>
        <div className="flex min-w-0 shrink-0 items-center justify-between gap-3 border-t border-border/40 px-3 py-3 sm:px-4">
          <div className="flex min-w-0 items-center gap-2"><Button type="button" variant="ghost" size="icon-sm" aria-label={expanded ? "Restore dialog size" : "Expand dialog"} onClick={() => setExpanded((value) => !value)}>{expanded ? <Minimize2Icon /> : <Maximize2Icon />}</Button><span className="hidden text-[11px] text-muted-foreground sm:block">{panel === "cluster" ? "Prepare your infrastructure" : panel === "runtime" ? "Settings apply to the Helm command" : state.enrollmentToken ? "Keep your enrollment token private" : "Enroll when your cluster is ready"}</span></div>
          <div className="flex shrink-0 items-center gap-2">
            <Button type="button" variant="ghost" size="sm" disabled={pending} onClick={() => { setOpen(false); onFinish() }}>Close</Button>
            {panel === "cluster" ? <Button type="button" size="sm" disabled={pending} onClick={() => setPanel("enrollment")}>Continue<ArrowRightIcon /></Button> : panel === "runtime" ? <Button type="button" size="sm" disabled={pending} onClick={() => setPanel("enrollment")}><ArrowLeftIcon />Enrollment</Button> : state.enrollmentToken ? <Button type="button" size="sm" disabled={pending} onClick={() => setPanel("runtime")}>Settings<ChevronRightIcon /></Button> : <Button type="submit" size="sm" disabled={pending || !!generationError}>{pending ? <Spinner /> : <KeyRoundIcon />}{pending ? "Generating…" : "Generate token"}</Button>}
          </div>
        </div>
      </form>
    </DialogContent>
  </Dialog>
}
