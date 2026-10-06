"use client"

import { useActionState, useId, useState } from "react"
import { CheckIcon, CopyIcon, ExternalLinkIcon, KeyRoundIcon, PlusIcon, ServerIcon, TerminalIcon } from "lucide-react"

import { createEnrollmentToken, type ActionState } from "@/app/(console)/actions"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
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

function CopyCommand({ command, label }: { command: string; label: string }) {
  const [copied, setCopied] = useState("")
  const [error, setError] = useState("")
  return <div className="overflow-hidden rounded-xl border bg-muted/40">
    <div className="flex items-center justify-between gap-3 border-b px-3 py-2">
      <span className="flex items-center gap-2 text-xs text-muted-foreground"><TerminalIcon className="size-3.5" />{label}</span>
      <Button type="button" variant="ghost" size="sm" aria-label={`Copy ${label}`} onClick={async () => {
        try { await navigator.clipboard.writeText(command); setCopied(command); setError("") }
        catch { setError("Copy is unavailable. Select the command below and copy it manually.") }
      }}>{copied === command ? <CheckIcon /> : <CopyIcon />}{copied === command ? "Copied" : "Copy"}</Button>
    </div>
    <pre tabIndex={0} aria-label={label} className="max-h-72 overflow-auto p-3 font-mono text-[11px] leading-6"><code>{command}</code></pre>
    {error ? <p role="status" className="px-3 pb-3 text-xs text-destructive">{error}</p> : null}
  </div>
}

export function EnrollmentTokenDialog({ disabled = false, config }: { disabled?: boolean; config: StampEnrollmentConfig }) {
  const [session, setSession] = useState(0)
  return <EnrollmentSession key={session} disabled={disabled} config={config} onFinish={() => setSession((value) => value + 1)} />
}

function EnrollmentSession({ disabled, config, onFinish }: { disabled: boolean; config: StampEnrollmentConfig; onFinish: () => void }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [state, action, pending] = useActionState<ActionState, FormData>(createEnrollmentToken, {})
  const [nvidia, setNvidia] = useState(true)
  const [setup, setSetup] = useState<StampSetup>({
    target: "k3s",
    stampName: "gpu-stamp",
    controlPlaneUrl: config.controlPlaneUrl,
    jwtIssuer: config.jwtIssuer,
    kubeContext: "",
    runtimeClass: "nvidia",
    modelImage: "vllm/vllm-openai:v0.26.0",
    gatewayHost: "",
    ingressClass: "traefik",
    certificateIssuer: "",
  })
  const update = (key: keyof StampSetup, value: string) => setSetup((previous) => ({ ...previous, [key]: value }))
  const effectiveSetup = { ...setup, jwtIssuer: state.enrollmentIssuer || setup.jwtIssuer }
  // The server supplies the actual token issuer after creating the enrollment token.
  const setupError = stampSetupError(effectiveSetup, false)
  const bootstrap = k3sBootstrapCommand(setup.stampName, nvidia)
  const helm = state.enrollmentToken ? stampHelmCommand(effectiveSetup, state.enrollmentToken) : null
  const commandError = state.enrollmentToken ? stampSetupError(effectiveSetup) : setupError

  return <Dialog open={open} onOpenChange={(next) => {
    if (pending) return
    setOpen(next)
    if (!next) onFinish()
  }}>
    <DialogTrigger render={<Button disabled={disabled}><PlusIcon /> Enroll stamp</Button>} />
    <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-3xl" showCloseButton={!pending}>
      <DialogHeader>
        <DialogTitle>Enroll an inference stamp</DialogTitle>
        <DialogDescription>Prepare your cluster, then install the Fabric operator to connect its capacity to this account.</DialogDescription>
      </DialogHeader>
      <form action={state.enrollmentToken ? undefined : action} onSubmit={state.enrollmentToken ? (event) => event.preventDefault() : undefined} className="space-y-5">
        <input type="hidden" name="mode" value="byoi" />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field><FieldLabel htmlFor={`${id}-target`}>Cluster</FieldLabel><Select value={setup.target} disabled={pending} onValueChange={(value) => {
            if (value !== "k3s" && value !== "kubernetes") return
            setSetup((previous) => ({ ...previous, target: value, runtimeClass: value === "k3s" ? (nvidia ? "nvidia" : "") : "", ingressClass: value === "k3s" ? "traefik" : "" }))
          }}><SelectTrigger id={`${id}-target`} className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="k3s">Create k3s on a VM</SelectItem><SelectItem value="kubernetes">Existing Kubernetes cluster</SelectItem></SelectContent></Select></Field>
          <Field><FieldLabel htmlFor={`${id}-name`}>Stamp name</FieldLabel><Input id={`${id}-name`} value={setup.stampName} onChange={(event) => update("stampName", event.target.value)} maxLength={53} disabled={pending} required /></Field>
        </div>

        {setup.target === "k3s" ? <section className="space-y-3 rounded-xl border p-4" aria-labelledby={`${id}-vm-title`}>
          <h3 id={`${id}-vm-title`} className="flex items-center gap-2 font-medium"><ServerIcon className="size-4 text-muted-foreground" /><span className="text-muted-foreground">1.</span> Create a k3s cluster on your VM</h3>
          <p className="text-xs leading-5 text-muted-foreground">Run this on an Ubuntu 22.04 or 24.04 VM. It installs k3s and Helm, with Traefik and local storage. Existing local k3s installations are reused.</p>
          <label className="flex items-center gap-2 text-xs"><Checkbox checked={nvidia} disabled={pending} onCheckedChange={(checked) => {
            setNvidia(checked === true)
            update("runtimeClass", checked === true ? "nvidia" : "")
          }} />Enable NVIDIA GPU support</label>
          {nvidia ? <p className="text-xs leading-5 text-muted-foreground">Install the correct NVIDIA driver first; <code>nvidia-smi</code> must work. The script adds the container toolkit, device plugin and measured GPU labels.</p> : null}
          {bootstrap ? <CopyCommand command={bootstrap} label="VM setup command" /> : <FieldError>Enter a valid stamp name to see the setup command.</FieldError>}
          <a href={K3S_BOOTSTRAP_URL} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">View the GitHub script<ExternalLinkIcon className="size-3" /></a>
        </section> : <Field><FieldLabel htmlFor={`${id}-context`}>Kubectl context</FieldLabel><Input id={`${id}-context`} value={setup.kubeContext} onChange={(event) => update("kubeContext", event.target.value)} placeholder="Your cluster context from kubectl config get-contexts" disabled={pending} required /><FieldDescription>Run the Helm command on a machine with Helm, this context, cluster-admin access and working GPU support.</FieldDescription></Field>}

        <section className="space-y-4 rounded-xl border p-4" aria-labelledby={`${id}-helm-title`}>
          <div className="space-y-1"><h3 id={`${id}-helm-title`} className="font-medium"><span className="mr-2 text-muted-foreground">{setup.target === "k3s" ? "2." : "1."}</span>Install the Fabric stamp with Helm</h3><p className="text-xs leading-5 text-muted-foreground">The operator registers this cluster and reconciles model deployments you create in the dashboard.</p></div>
          <Field><FieldLabel htmlFor={`${id}-control`}>Control-plane address</FieldLabel><Input id={`${id}-control`} type="url" value={setup.controlPlaneUrl} onChange={(event) => update("controlPlaneUrl", event.target.value)} placeholder="https://control.your-domain.com" disabled={pending} required /><FieldDescription>Use the HTTPS address reachable from this VM or cluster.</FieldDescription></Field>
          <p className="text-xs leading-5 text-muted-foreground">Model runtime: <code className="break-all">{setup.modelImage}</code>. Standard vLLM 0.26 uses CUDA 13 and needs an R580 or newer driver. For A10 GRID 570 / CUDA 12.8, choose vLLM 0.11 below for its supported models.</p>
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-muted/50 px-3 py-2 text-xs"><span>Helm chart: <code>deploy/helm/fabric-stamp</code></span><a href={`${STAMP_REPOSITORY}/tree/${STAMP_CHART_REF}/deploy/helm/fabric-stamp`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground">Chart reference<ExternalLinkIcon className="size-3" /></a></div>

          <details className="rounded-lg border p-3"><summary className="cursor-pointer text-xs font-medium">Runtime and inference gateway settings</summary><div className="mt-4 space-y-4">
            <Field><FieldLabel htmlFor={`${id}-image`}>vLLM image</FieldLabel><Input id={`${id}-image`} value={setup.modelImage} onChange={(event) => update("modelImage", event.target.value)} disabled={pending} /><FieldDescription>Choose an image compatible with the GPU driver and model. For the A10 GRID 570 / CUDA 12.8 setup, use <code>vllm/vllm-openai:v0.11.0</code> for supported models. Qwen3.5 needs a newer vLLM image and a compatible driver.</FieldDescription></Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field><FieldLabel htmlFor={`${id}-runtime`}>GPU RuntimeClass</FieldLabel><Input id={`${id}-runtime`} value={setup.runtimeClass} onChange={(event) => update("runtimeClass", event.target.value)} placeholder="Leave empty for the default runtime" disabled={pending} /></Field>
              <Field><FieldLabel htmlFor={`${id}-issuer`}>Control-plane JWT issuer</FieldLabel><Input id={`${id}-issuer`} value={effectiveSetup.jwtIssuer} onChange={(event) => update("jwtIssuer", event.target.value)} placeholder="Detected when the token is generated" disabled={pending || !!state.enrollmentIssuer} /></Field>
            </div>
            <Field><FieldLabel htmlFor={`${id}-gateway`}>Inference gateway hostname (optional)</FieldLabel><Input id={`${id}-gateway`} value={setup.gatewayHost} onChange={(event) => update("gatewayHost", event.target.value)} placeholder="inference.your-domain.com" disabled={pending} /><FieldDescription>Point DNS at the ingress controller. Leave empty to enroll first and configure public inference later.</FieldDescription></Field>
            {setup.gatewayHost ? <div className="grid gap-4 sm:grid-cols-2">
              <Field><FieldLabel htmlFor={`${id}-ingress`}>IngressClass</FieldLabel><Input id={`${id}-ingress`} value={setup.ingressClass} onChange={(event) => update("ingressClass", event.target.value)} disabled={pending} /></Field>
              <Field><FieldLabel htmlFor={`${id}-certificate`}>Existing ClusterIssuer</FieldLabel><Input id={`${id}-certificate`} value={setup.certificateIssuer} onChange={(event) => update("certificateIssuer", event.target.value)} placeholder="Your cert-manager ClusterIssuer" disabled={pending} /></Field>
            </div> : null}
          </div></details>

          {state.enrollmentToken ? <div className="space-y-3">
            <Alert><KeyRoundIcon /><AlertTitle>Enrollment command ready</AlertTitle><AlertDescription>This command includes your single-use token, which expires {state.expiresAt ? new Date(state.expiresAt).toLocaleString() : "soon"}. Keep it private. Closing this dialog clears it.</AlertDescription></Alert>
            {commandError ? <FieldError>{commandError}</FieldError> : null}
            {helm ? <CopyCommand command={helm} label="Helm enrollment command" /> : null}
            <p className="text-xs leading-5 text-muted-foreground">The token is stored in the Helm release and enrollment Secret. Run this once for a new stamp. The agent keeps its enrolled identity on persistent storage.</p>
            {setup.gatewayHost ? <p className="text-xs leading-5 text-muted-foreground">After a model placement is ready, the stamp reports <code className="break-all">https://{setup.gatewayHost}</code> to Fabric. Approve this origin in the console&apos;s inference gateway configuration to use the playground.</p> : <p className="text-xs leading-5 text-muted-foreground">This enrolls the cluster. Public inference and playground access also need a gateway hostname and TLS.</p>}
          </div> : <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
            <Field><FieldLabel htmlFor={`${id}-expiry`}>Token expires in (minutes)</FieldLabel><Input id={`${id}-expiry`} name="expiresInMinutes" type="number" min={5} max={1440} defaultValue={60} disabled={pending} required /><FieldDescription>Generate it after the cluster is ready. Valid for 5 minutes to 24 hours.</FieldDescription></Field>
            <Button type="submit" disabled={pending || !!setupError}>{pending ? <Spinner /> : <KeyRoundIcon />}{pending ? "Generating…" : "Generate token and command"}</Button>
          </div>}
          {!state.enrollmentToken && setupError ? <p role="status" className="text-xs text-muted-foreground">{setupError}</p> : null}
          {state.error ? <FieldError>{state.error}</FieldError> : null}
        </section>
        <DialogFooter><Button type="button" variant="outline" disabled={pending} onClick={() => { setOpen(false); onFinish() }}>Close</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
