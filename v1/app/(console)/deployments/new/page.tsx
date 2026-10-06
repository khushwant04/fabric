import Link from "next/link"
import { ArrowLeftIcon, RocketIcon } from "lucide-react"

import { DeploymentForm, DeployModelSubmit } from "@/components/console/deployment-form"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { listStamps } from "@/lib/fabric/data"
import { gpuCapacity, stampState } from "@/lib/fabric/resource-state"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"

const gpuClasses = ["t4", "v100", "rtx-a4000", "l4", "a10", "a10g", "a100", "a100-80gb", "l40s", "h100"]

export default async function NewDeploymentPage() {
  const context = await getConsoleContext()
  const canWrite = hasScope(context, "deployments:write")
  const result = await Promise.allSettled([hasScope(context, "stamps:read") ? listStamps() : Promise.resolve([])])
  const stamps = result[0].status === "fulfilled" ? result[0].value : []
  const active = stamps.filter((stamp) => stampState(stamp).status === "active")
  return <PageContainer>
    <PageHeader title="Deploy a model" description="Choose a model and enrolled infrastructure. Fabric creates and manages its GPU workload." actions={<Button variant="outline" render={<Link href="/deployments" />}><ArrowLeftIcon /> Back</Button>} />
    {!canWrite ? <Alert><RocketIcon /><AlertTitle>Read-only access</AlertTitle><AlertDescription>Your account role does not allow deploying models.</AlertDescription></Alert> : null}
    {result[0].status === "rejected" ? <Alert variant="destructive"><RocketIcon /><AlertTitle>Infrastructure could not be checked</AlertTitle><AlertDescription>Refresh before deploying. No capacity has been assumed available.</AlertDescription></Alert> : null}
    <DeploymentForm>
      <Card><CardHeader><CardTitle>Model and infrastructure</CardTitle><CardDescription>Authentication, TLS, and gateway settings come from your Helm installation.</CardDescription></CardHeader><CardContent><FieldGroup>
        <Field><FieldLabel htmlFor="modelRef">Model repository</FieldLabel><Input id="modelRef" name="modelRef" placeholder="Qwen/Qwen3.5-4B" required maxLength={200} disabled={!canWrite} /><FieldDescription>The model-host image must support this model architecture. Gated models need a download credential configured on the cluster.</FieldDescription></Field>
        <div className="grid gap-5 md:grid-cols-2">
          <Field><FieldLabel htmlFor="name">Deployment name</FieldLabel><Input id="name" name="name" placeholder="qwen-4b" pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?" maxLength={200} required disabled={!canWrite} /></Field>
          <Field><FieldLabel htmlFor="modelAlias">API model name</FieldLabel><Input id="modelAlias" name="modelAlias" placeholder="qwen3.5-4b" pattern="[a-zA-Z0-9_.-]+" maxLength={200} required disabled={!canWrite} /><FieldDescription>Use this name in the OpenAI request’s model field.</FieldDescription></Field>
        </div>
        <Field><FieldLabel>Infrastructure</FieldLabel><Select name="stampId" defaultValue="auto" disabled={!canWrite}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="auto">Automatic placement</SelectItem>{active.map((stamp) => <SelectItem key={stamp.id} value={stamp.id}>{stamp.name} · {stamp.orchestrator || "Kubernetes"} · {gpuCapacity(stamp.capabilities.allocatable_gpus) ?? "Unknown"} GPUs</SelectItem>)}</SelectContent></Select><FieldDescription>{active.length ? "Only recently connected clusters appear here. The control plane checks available capacity before assigning the model." : "No active account-owned cluster is visible. Enroll a stamp first, or use entitled managed capacity through automatic placement."}</FieldDescription></Field>
        <div className="grid gap-5 sm:grid-cols-3">
          <Field><FieldLabel htmlFor="replicas">Replicas</FieldLabel><Input id="replicas" name="replicas" type="number" min={1} max={32} defaultValue={1} required disabled={!canWrite} /></Field>
          <Field><FieldLabel htmlFor="gpuCount">GPUs per replica</FieldLabel><Input id="gpuCount" name="gpuCount" type="number" min={1} max={8} defaultValue={1} required disabled={!canWrite} /><FieldDescription>All GPUs for one replica must fit on one node.</FieldDescription></Field>
          <Field><FieldLabel>Minimum GPU class</FieldLabel><Select name="gpuClass" defaultValue="t4" disabled={!canWrite}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent>{gpuClasses.map((value) => <SelectItem value={value} key={value}>{value.toUpperCase()}</SelectItem>)}</SelectContent></Select></Field>
        </div>
        <details className="rounded-lg border p-4"><summary className="cursor-pointer text-sm font-medium">Serving settings</summary><div className="mt-5 grid gap-5 sm:grid-cols-3">
          <Field><FieldLabel htmlFor="maxModelLen">Context limit</FieldLabel><Input id="maxModelLen" name="maxModelLen" type="number" min={64} max={1048576} placeholder="Cluster default" disabled={!canWrite} /></Field>
          <Field><FieldLabel htmlFor="maxNumSeqs">Concurrent sequences</FieldLabel><Input id="maxNumSeqs" name="maxNumSeqs" type="number" min={1} max={1024} placeholder="Cluster default" disabled={!canWrite} /></Field>
          <Field><FieldLabel>Execution</FieldLabel><Select name="execution" defaultValue="default" disabled={!canWrite}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">Cluster default</SelectItem><SelectItem value="eager">Eager</SelectItem><SelectItem value="cuda_graph">CUDA graphs</SelectItem></SelectContent></Select></Field>
          <Field><FieldLabel>Automatic tool calling</FieldLabel><Select name="autoToolChoice" defaultValue="default" disabled={!canWrite}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">Model default</SelectItem><SelectItem value="enabled">Enabled</SelectItem><SelectItem value="disabled">Disabled</SelectItem></SelectContent></Select><FieldDescription>Allow the model to select tools supplied in an API request.</FieldDescription></Field>
          <Field className="sm:col-span-2"><FieldLabel htmlFor="toolCallParser">Tool parser</FieldLabel><Input id="toolCallParser" name="toolCallParser" placeholder="Model default" maxLength={64} pattern="[a-z][a-z0-9_-]*" disabled={!canWrite} /><FieldDescription>Known Qwen models select a compatible parser automatically. Use hermes for Qwen3-VL or qwen3_xml for Qwen3.5. Other models need a parser supported by the model-host image.</FieldDescription></Field>
        </div><p className="mt-4 text-xs text-muted-foreground">The operator selects a supported dtype from the GPU profile. Model memory fit still depends on model size, context, and serving settings.</p></details>
      </FieldGroup></CardContent></Card>
      <Card className="h-fit"><CardHeader><CardTitle>What happens next</CardTitle></CardHeader><CardContent className="space-y-5"><ol className="list-decimal space-y-3 pl-4 text-sm text-muted-foreground"><li>The control plane validates and assigns the deployment.</li><li>The stamp agent delivers it to the operator.</li><li>The operator starts the model host on a compatible GPU node.</li><li>The gateway routes requests when the host is ready.</li></ol><p className="text-xs text-muted-foreground">The inference URL appears on the deployment when the stamp reports readiness. A single-GPU node can start one single-GPU model; replacing an occupied model requires spare capacity or stopping it first.</p><DeployModelSubmit disabled={!canWrite || result[0].status === "rejected"} /></CardContent></Card>
    </DeploymentForm>
  </PageContainer>
}
