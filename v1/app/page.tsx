import { redirect } from "next/navigation"
import { ArrowRightIcon, BoxesIcon, CpuIcon, KeyRoundIcon, ShieldCheckIcon } from "lucide-react"

import { IconTile } from "@/components/console/section"
import { FabricLogo } from "@/components/fabric-mark"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { isAuth0Configured } from "@/lib/auth0"
import { getAuthSession } from "@/lib/fabric/session"

const highlights = [
  { icon: BoxesIcon, title: "Deployments", description: "Declare a model once; Fabric places and serves it." },
  { icon: CpuIcon, title: "Inference stamps", description: "Your GPU clusters, connected by an agent." },
  { icon: KeyRoundIcon, title: "Access", description: "Scoped keys, members and your own identity provider." },
]

export default async function Home({ searchParams }: PageProps<"/">) {
  const query = await searchParams
  const session = await getAuthSession()
  if (session) redirect("/onboarding")

  const configurationRequired = query.configuration === "required" || !isAuth0Configured

  return (
    <main className="grid min-h-svh bg-background lg:grid-cols-[1.05fr_0.95fr]">
      <section className="fabric-page-content hidden flex-col justify-between border-r border-border p-12 lg:flex">
        <FabricLogo />
        <div className="flex max-w-xl flex-col gap-10">
          <div className="flex flex-col gap-4">
            <span className="w-fit font-mono text-[11px] font-medium tracking-[0.06em] text-muted-foreground uppercase">Operator console</span>
            <h1 className="text-[44px] leading-[1.1] font-medium tracking-[-0.035em] text-balance">One control surface for your <span className="fabric-highlight">inference fleet</span>.</h1>
            <p className="max-w-lg text-base leading-7 text-muted-foreground">Deploy models, connect GPU infrastructure, inspect operational usage and manage access from one governed workspace.</p>
          </div>
          <ul className="fabric-brackets flex flex-col divide-y divide-border border border-border bg-card">
            {highlights.map((item) => <li key={item.title} className="flex items-start gap-4 px-5 py-4">
              <IconTile><item.icon /></IconTile>
              <span><span className="block text-[15px] font-medium">{item.title}</span><span className="block text-sm text-muted-foreground">{item.description}</span></span>
            </li>)}
          </ul>
        </div>
        <p className="font-mono text-[12px] text-muted-foreground">Control-plane credentials stay server-side.</p>
      </section>
      <section className="flex flex-col items-center justify-center gap-8 bg-card p-6 sm:p-10">
        <FabricLogo className="lg:hidden" />
        <div className="fabric-brackets w-full max-w-sm border border-border bg-background p-8">
          <IconTile tone="neutral" className="mb-6 size-10"><ShieldCheckIcon /></IconTile>
          <h2 className="text-2xl leading-8 font-medium tracking-[-0.02em]">Sign in to Fabric</h2>
          <p className="mt-1.5 text-sm text-muted-foreground">Authenticate with your organization to open the operator console.</p>
          <div className="mt-7 flex flex-col gap-5">
            {configurationRequired ? <Alert><ShieldCheckIcon /><AlertTitle>Configuration required</AlertTitle><AlertDescription>Configure organization sign-in and the Fabric control plane to access this console.</AlertDescription></Alert> : <Button size="lg" nativeButton={false} render={<a href="/auth/login?returnTo=/onboarding" />} className="w-full">Continue with SSO <ArrowRightIcon /></Button>}
            <p className="border-t border-border pt-5 text-center text-xs leading-5 text-muted-foreground">Access is restricted to authorized Fabric operators. Authentication events may be audited.</p>
          </div>
        </div>
      </section>
    </main>
  )
}
