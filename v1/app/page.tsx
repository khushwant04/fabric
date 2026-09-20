import { redirect } from "next/navigation"
import { ArrowRightIcon, BoxesIcon, CpuIcon, KeyRoundIcon, ShieldCheckIcon } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { isAuth0Configured } from "@/lib/auth0"
import { getAuthSession, isDemoMode } from "@/lib/fabric/session"

export default async function Home({ searchParams }: PageProps<"/">) {
  const query = await searchParams
  if (isDemoMode) redirect("/dashboard")
  const session = await getAuthSession()
  if (session) redirect("/onboarding")

  const configurationRequired = query.configuration === "required" || !isAuth0Configured

  return (
    <main className="grid min-h-svh bg-muted/30 lg:grid-cols-[1.1fr_0.9fr]">
      <section className="hidden border-r bg-background p-12 lg:flex lg:flex-col lg:justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold"><span className="flex size-7 items-center justify-center rounded-md border shadow-xs">F</span> Fabric</div>
        <div className="max-w-xl space-y-7">
          <div className="space-y-3"><p className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground">Operator console</p><h1 className="text-4xl font-semibold tracking-tight">One control surface for your inference fleet.</h1><p className="max-w-lg text-base leading-7 text-muted-foreground">Deploy models, connect GPU infrastructure, inspect operational usage, and manage access from one governed workspace.</p></div>
          <div className="grid grid-cols-3 gap-3">
            {[{ icon: BoxesIcon, label: "Deployments" }, { icon: CpuIcon, label: "Stamps" }, { icon: KeyRoundIcon, label: "Access" }].map((item) => <Card size="sm" key={item.label}><CardContent className="gap-2"><item.icon className="size-4 text-muted-foreground" /><span className="text-xs font-medium">{item.label}</span></CardContent></Card>)}
          </div>
        </div>
        <p className="text-xs text-muted-foreground">Control-plane credentials stay server-side.</p>
      </section>
      <section className="flex items-center justify-center p-6 sm:p-10">
        <Card className="w-full max-w-md">
          <CardHeader><div className="mb-4 flex size-10 items-center justify-center rounded-lg border bg-muted"><ShieldCheckIcon className="size-5" /></div><CardTitle className="text-xl">Sign in to Fabric</CardTitle><CardDescription>Authenticate with your organization to access the operator console.</CardDescription></CardHeader>
          <CardContent className="gap-5">
            {configurationRequired ? <Alert><ShieldCheckIcon /><AlertTitle>Configuration required</AlertTitle><AlertDescription>Add the Auth0 and control-plane variables from <code className="font-mono text-xs">.env.example</code>. For local visual review only, set <code className="font-mono text-xs">FABRIC_DEMO_MODE=true</code>.</AlertDescription></Alert> : <Button size="lg" render={<a href="/auth/login?returnTo=/onboarding" />} className="w-full">Continue with SSO <ArrowRightIcon /></Button>}
            <Separator />
            <p className="text-center text-xs leading-5 text-muted-foreground">Access is restricted to authorized Fabric operators. Authentication events may be audited.</p>
          </CardContent>
        </Card>
      </section>
    </main>
  )
}
