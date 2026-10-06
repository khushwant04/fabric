"use client"

import { useState } from "react"
import { ArrowDownIcon, ArrowRightIcon, BoxIcon, CpuIcon, GlobeIcon, NetworkIcon, ServerIcon, ShieldCheckIcon } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"

const flows = {
  deploy: {
    label: "Deploy a model", subtitle: "The control plane declares intent. Kubernetes schedules and runs it.",
    steps: [
      { name: "Console / API", icon: GlobeIcon, zone: "Account", detail: "Create a model deployment and choose a placement. Your account owns the deployment and controls who can change it." },
      { name: "Control plane", icon: ShieldCheckIcon, zone: "Fabric", detail: "Authorize the request, record the desired generation and assign it to an eligible stamp. Registering a model alone does not enroll a cluster." },
      { name: "Stamp agent", icon: NetworkIcon, zone: "Your cluster", detail: "Poll the control plane outbound using the stamp's machine identity, then publish the desired model deployment inside the cluster." },
      { name: "Operator", icon: BoxIcon, zone: "Your cluster", detail: "Reconcile model workloads, services and gateway routes. Applied configuration and a ready model host are separate states." },
      { name: "Kubernetes / GPU", icon: CpuIcon, zone: "Your cluster", detail: "Schedule a replica onto an eligible GPU node. The host pulls its image, loads cached or downloaded weights, profiles the engine and passes its readiness probe." },
    ],
  },
  route: {
    label: "Route a request", subtitle: "Authentication, model choice and backend balancing happen before generation.",
    steps: [
      { name: "Application", icon: GlobeIcon, zone: "Client", detail: "Send an OpenAI-compatible request with an inference token. Pin an exact alias or select auto with an optional task hint." },
      { name: "Stamp gateway", icon: ShieldCheckIcon, zone: "Your cluster", detail: "Verify token audience, issuer and account scope. Apply admission limits and reject unauthorized model access." },
      { name: "Model / backend", icon: NetworkIcon, zone: "Your cluster", detail: "Auto uses declared capabilities and a task heuristic to choose a model within this stamp. Backend routing follows the deployment's configured strategy, including least in flight." },
      { name: "vLLM host", icon: CpuIcon, zone: "GPU node", detail: "Batch accepted requests, run the model and stream generated output. A request can explicitly ask for routing explanations without exposing backend addresses." },
      { name: "Usage collector", icon: ServerIcon, zone: "Your cluster", detail: "Collect completed request usage and send it to the control plane with separate telemetry credentials. The console reads observed deployment status and account usage." },
    ],
  },
  join: {
    label: "Join a cluster", subtitle: "Enroll a stamp once. GPU workers join through the cluster's native workflow.",
    steps: [
      { name: "Enrollment token", icon: ShieldCheckIcon, zone: "Account", detail: "Create a single-use account-bound enrollment token. Install a stamp on existing Kubernetes or k3s capacity." },
      { name: "Stamp identity", icon: BoxIcon, zone: "Your cluster", detail: "The agent enrolls and stores its identity on a persistent volume. Restarting the agent retains the same stamp; losing that identity needs an explicit recovery workflow." },
      { name: "GPU worker", icon: CpuIcon, zone: "Your cluster", detail: "Join a worker using AKS or your Kubernetes distribution. Install the GPU driver and device plugin and match the stamp's node selector." },
      { name: "Capacity report", icon: NetworkIcon, zone: "Fabric", detail: "The existing stamp refreshes pool capacity. An added worker does not create another stamp or automatically increase model replicas." },
      { name: "Placement / replicas", icon: ServerIcon, zone: "Account", detail: "Place a new model or update declared replicas to use the capacity. Kubernetes schedules the Pods; Fabric does not provision nodes or provide cross-cluster automatic failover." },
    ],
  },
} as const

type Flow = keyof typeof flows

export function ArchitectureExplorer() {
  const [flow, setFlow] = useState<Flow>("deploy")
  const [step, setStep] = useState(0)
  const selected = flows[flow]
  const current = selected.steps[step]
  return (
    <div className="space-y-6">
      <Tabs value={flow} onValueChange={(value) => { setFlow(value as Flow); setStep(0) }}>
        <TabsList className="h-auto flex-wrap justify-start gap-1">
          {Object.entries(flows).map(([key, item]) => <TabsTrigger key={key} value={key}>{item.label}</TabsTrigger>)}
        </TabsList>
      </Tabs>
      <Card>
        <CardHeader><CardTitle>{selected.label}</CardTitle><p className="text-sm text-muted-foreground">{selected.subtitle}</p></CardHeader>
        <CardContent>
          <ol aria-label={`${selected.label} sequence`} className="flex flex-col gap-3 lg:flex-row lg:items-stretch">
            {selected.steps.map((item, index) => {
              const Icon = item.icon
              return <li key={item.name} className="flex min-w-0 flex-1 flex-col items-center gap-3 lg:flex-row">
                <button type="button" aria-pressed={step === index} onClick={() => setStep(index)} className={`flex min-h-32 w-full min-w-0 flex-1 flex-col items-start gap-3 rounded-xl border p-4 text-left transition-colors focus-visible:outline-2 focus-visible:outline-ring ${step === index ? "border-primary bg-primary/5" : "border-border bg-muted/30 hover:bg-muted"}`}>
                  <div className="flex w-full items-center justify-between"><Icon className="size-5" /><span className="text-xs text-muted-foreground">{index + 1}</span></div>
                  <span className="text-sm font-medium">{item.name}</span><span className="text-xs text-muted-foreground">{item.zone}</span>
                </button>
                {index < selected.steps.length - 1 ? <><ArrowRightIcon className="hidden size-4 shrink-0 text-muted-foreground lg:block" /><ArrowDownIcon className="size-4 text-muted-foreground lg:hidden" /></> : null}
              </li>
            })}
          </ol>
        </CardContent>
      </Card>
      <Card aria-live="polite"><CardHeader><CardTitle>Step {step + 1}: {current.name}</CardTitle></CardHeader><CardContent className="space-y-4"><p className="max-w-4xl text-sm leading-7 text-muted-foreground">{current.detail}</p><div className="flex gap-2"><Button variant="outline" disabled={step === 0} onClick={() => setStep(step - 1)}>Previous</Button><Button disabled={step === selected.steps.length - 1} onClick={() => setStep(step + 1)}>Next step<ArrowRightIcon /></Button></div></CardContent></Card>
      <div className="grid gap-4 md:grid-cols-3">
        {[ ["One stamp per GPU pool", "Use disjoint node selectors when multiple stamps share one cluster. Overlapping pools can advertise the same GPU twice."], ["Observed state matters", "A placement records intent. Ready replicas and current observed generations establish whether that intent is serving."], ["Startup and caching", "Fresh nodes need image pulls and weights. Engine profiling happens on startup; first request shapes may still require compilation."] ].map(([title, detail]) => <Card key={title}><CardHeader><CardTitle className="text-sm">{title}</CardTitle></CardHeader><CardContent className="text-sm leading-6 text-muted-foreground">{detail}</CardContent></Card>)}
      </div>
    </div>
  )
}
