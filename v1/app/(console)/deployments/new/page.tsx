import Link from "next/link"
import { ArrowLeftIcon, RocketIcon } from "lucide-react"

import { DeploySubmit, DeploymentForm, NewDeploymentFlow } from "@/components/console/deployment-form"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Section, SectionBody } from "@/components/console/section"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { listStamps } from "@/lib/fabric/data"
import { catalogModel, deploymentStampChoices } from "@/lib/fabric/model-catalog"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"

// The full-page picker for deep links and visits without JavaScript: each model is a link
// to ?model=<id>, which renders the confirmation step. In the console, "Create deployment"
// opens the same picker in a drawer (CreateDeploymentDrawer).
export default async function NewDeploymentPage({ searchParams }: PageProps<"/deployments/new">) {
  const query = await searchParams
  const context = await getConsoleContext()
  const canWrite = hasScope(context, "deployments:write")
  const result = await Promise.allSettled([hasScope(context, "stamps:read") ? listStamps() : Promise.resolve([])])
  const stamps = result[0].status === "fulfilled" ? result[0].value : []
  const requested = String(query.model ?? "")
  const selection = requested === "custom" ? "custom" : catalogModel(requested)?.id ?? null
  const blocked = result[0].status === "rejected"
  return <PageContainer>
    <PageHeader title="Deploy a model" description="Choose a model and where it runs. Fabric creates and manages its GPU workload." actions={<Button variant="outline" nativeButton={false} render={<Link href="/deployments" />}><ArrowLeftIcon /> Back</Button>} />
    {!canWrite ? <Alert><RocketIcon /><AlertTitle>Read-only access</AlertTitle><AlertDescription>Your account role does not allow deploying models.</AlertDescription></Alert> : null}
    {blocked ? <Alert variant="destructive"><RocketIcon /><AlertTitle>Infrastructure could not be checked</AlertTitle><AlertDescription>Refresh before deploying. No capacity has been assumed available.</AlertDescription></Alert> : null}
    <div className="w-full max-w-3xl">
      <DeploymentForm>
        <Section title={selection ? "Confirm and deploy" : "Choose a model"} description={selection ? "Authentication, TLS, and gateway settings come from your Helm installation." : undefined}>
          <SectionBody className="gap-6">
            <NewDeploymentFlow stamps={deploymentStampChoices(stamps)} selection={selection} linkBase="/deployments/new" disabled={!canWrite} />
            {selection ? <div className="flex flex-col gap-3 border-t border-border pt-5">
              <DeploySubmit disabled={!canWrite || blocked} className="w-full" />
              <p className="text-center text-xs text-muted-foreground">The stamp’s agent starts the model host; the gateway routes requests once it reports ready.</p>
            </div> : null}
          </SectionBody>
        </Section>
      </DeploymentForm>
    </div>
  </PageContainer>
}
