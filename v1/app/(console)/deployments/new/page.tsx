import Link from "next/link"
import { ArrowLeftIcon, RocketIcon } from "lucide-react"

import { createDeployment } from "@/app/(console)/actions"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"

const gpuClasses = ["t4", "l4", "a10g", "a100", "a100-80gb", "l40s", "h100"]

export default async function NewDeploymentPage() {
  const context = await getConsoleContext()
  const canWrite = hasScope(context, "deployments:write") && !context.demo
  return <PageContainer><PageHeader title="Create deployment" description="Define desired model-serving state. Fabric will select compatible capacity and begin reconciliation." actions={<Button variant="outline" render={<Link href="/deployments" />}><ArrowLeftIcon /> Back</Button>} />
    {!canWrite ? <Alert><RocketIcon /><AlertTitle>Read-only preview</AlertTitle><AlertDescription>Connect a live account with deployments:write to submit this form.</AlertDescription></Alert> : null}
    <form action={createDeployment} className="grid gap-5 xl:grid-cols-[1fr_320px]">
      <Card><CardHeader><CardTitle>Deployment configuration</CardTitle><CardDescription>Required fields are validated again by the control plane.</CardDescription></CardHeader><CardContent><FieldGroup>
        <FieldSet><FieldLegend>Identity</FieldLegend><div className="grid gap-5 md:grid-cols-2"><Field><FieldLabel htmlFor="name">Deployment name</FieldLabel><Input id="name" name="name" placeholder="support-copilot" pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?" required disabled={!canWrite} /><FieldDescription>Lowercase letters, numbers, and hyphens.</FieldDescription></Field><Field><FieldLabel htmlFor="modelAlias">Model alias</FieldLabel><Input id="modelAlias" name="modelAlias" placeholder="llama-3.1-8b-instruct" required disabled={!canWrite} /></Field></div></FieldSet>
        <FieldSet><FieldLegend>Runtime</FieldLegend><div className="grid gap-5 md:grid-cols-3"><Field><FieldLabel htmlFor="release">Runtime release</FieldLabel><Input id="release" name="release" placeholder="v0.9.4" required disabled={!canWrite} /></Field><Field><FieldLabel>Kernel mode</FieldLabel><Select name="kernelMode" defaultValue="auto" disabled={!canWrite}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent>{["auto", "fabric", "standard"].map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent></Select></Field><Field><FieldLabel>Load strategy</FieldLabel><Select name="strategy" defaultValue="least_in_flight" disabled={!canWrite}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent>{["least_in_flight", "round_robin", "session_affinity", "weighted"].map((value) => <SelectItem value={value} key={value}>{value.replaceAll("_", " ")}</SelectItem>)}</SelectContent></Select></Field></div></FieldSet>
        <FieldSet><FieldLegend>Resources and placement</FieldLegend><div className="grid gap-5 md:grid-cols-2 lg:grid-cols-4"><Field><FieldLabel htmlFor="replicas">Replicas</FieldLabel><Input id="replicas" name="replicas" type="number" min={1} max={32} defaultValue={1} required disabled={!canWrite} /></Field><Field><FieldLabel htmlFor="gpuCount">GPUs per replica</FieldLabel><Input id="gpuCount" name="gpuCount" type="number" min={1} max={8} defaultValue={1} required disabled={!canWrite} /></Field><Field><FieldLabel>Minimum GPU</FieldLabel><Select name="gpuClass" defaultValue="t4" disabled={!canWrite}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent>{gpuClasses.map((value) => <SelectItem value={value} key={value}>{value.toUpperCase()}</SelectItem>)}</SelectContent></Select></Field><Field><FieldLabel htmlFor="region">Region (optional)</FieldLabel><Input id="region" name="region" placeholder="us-east-1" disabled={!canWrite} /></Field></div><Field><FieldLabel htmlFor="limitsPolicy">Limits policy reference (optional)</FieldLabel><Input id="limitsPolicy" name="limitsPolicy" placeholder="standard-production" disabled={!canWrite} /></Field></FieldSet>
      </FieldGroup></CardContent></Card>
      <Card className="h-fit"><CardHeader><CardTitle>Review</CardTitle><CardDescription>Creation is idempotent. Placement begins immediately after the intent is accepted.</CardDescription></CardHeader><CardContent><div className="space-y-3 rounded-lg border p-3 text-xs text-muted-foreground"><p>• Deployment specs are account-scoped.</p><p>• Automatic placement checks entitlement, region, liveness, GPU class, and available capacity.</p><p>• Usage reporting is operational, not billing-grade.</p></div><Button className="w-full" type="submit" disabled={!canWrite}><RocketIcon /> Create and place</Button></CardContent></Card>
    </form>
  </PageContainer>
}
