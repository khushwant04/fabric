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
      <section className="hidden flex-col justify-between border-r border-border bg-muted/50 p-12 lg:flex">
        <FabricLogo />
        <div className="flex max-w-xl flex-col gap-10">
          <div className="flex flex-col gap-4">
            <span className="w-fit rounded-full bg-brand-tint px-2.5 py-1 text-xs font-medium text-brand-foreground">Operator console</span>
            <h1 className="text-[40px] leading-[1.1] font-medium tracking-[-0.03em] text-balance">One control surface for your inference fleet.</h1>
            <p className="max-w-lg text-base leading-7 text-muted-foreground">Deploy models, connect GPU infrastructure, inspect operational usage and manage access from one governed workspace.</p>
          </div>
          <ul className="flex flex-col gap-4">
            {highlights.map((item) => <li key={item.title} className="flex items-start gap-4">
              <IconTile><item.icon /></IconTile>
              <span><span className="block text-[15px] font-medium">{item.title}</span><span className="block text-sm text-muted-foreground">{item.description}</span></span>
            </li>)}
          </ul>
        </div>
        <p className="text-sm text-muted-foreground">Control-plane credentials stay server-side.</p>
      </section>
      <section className="flex flex-col items-center justify-center gap-8 p-6 sm:p-10">
        <FabricLogo className="lg:hidden" />
        <div className="w-full max-w-sm rounded-2xl border border-border bg-card p-8 shadow-[0_1px_2px_rgb(0_0_0/0.03)]">
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
