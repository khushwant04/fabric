import Link from "next/link"
import { ArrowLeftIcon, RocketIcon } from "lucide-react"

import { DeploymentFields, DeploymentForm, DeploymentNextSteps, DeployModelSubmit } from "@/components/console/deployment-form"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { listStamps } from "@/lib/fabric/data"
import { deploymentStampOptions } from "@/lib/fabric/resource-state"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"

// The full-page form for deep links and visits without JavaScript. In the console,
// "Create deployment" opens the same fields in a drawer (CreateDeploymentDrawer).
export default async function NewDeploymentPage() {
  const context = await getConsoleContext()
  const canWrite = hasScope(context, "deployments:write")
  const result = await Promise.allSettled([hasScope(context, "stamps:read") ? listStamps() : Promise.resolve([])])
  const stamps = result[0].status === "fulfilled" ? result[0].value : []
  return <PageContainer>
    <PageHeader title="Deploy a model" description="Choose a model and enrolled infrastructure. Fabric creates and manages its GPU workload." actions={<Button variant="outline" render={<Link href="/deployments" />}><ArrowLeftIcon /> Back</Button>} />
    {!canWrite ? <Alert><RocketIcon /><AlertTitle>Read-only access</AlertTitle><AlertDescription>Your account role does not allow deploying models.</AlertDescription></Alert> : null}
    {result[0].status === "rejected" ? <Alert variant="destructive"><RocketIcon /><AlertTitle>Infrastructure could not be checked</AlertTitle><AlertDescription>Refresh before deploying. No capacity has been assumed available.</AlertDescription></Alert> : null}
    <DeploymentForm>
      <Card><CardHeader><CardTitle>Model and infrastructure</CardTitle><CardDescription>Authentication, TLS, and gateway settings come from your Helm installation.</CardDescription></CardHeader><CardContent>
        <DeploymentFields stamps={deploymentStampOptions(stamps)} disabled={!canWrite} />
      </CardContent></Card>
      <Card className="h-fit"><CardHeader><CardTitle>What happens next</CardTitle></CardHeader><CardContent className="space-y-5"><DeploymentNextSteps /><DeployModelSubmit disabled={!canWrite || result[0].status === "rejected"} /></CardContent></Card>
    </DeploymentForm>
  </PageContainer>
}
