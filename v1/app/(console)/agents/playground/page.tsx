import Link from "next/link"
import { InfoIcon, ListTreeIcon } from "lucide-react"

import { AgentPlayground } from "@/components/agents/agent-playground"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { getAgentPlaygroundOptions } from "@/lib/fabric/agent-data"

export default async function AgentPlaygroundPage() {
  const { options, notice, vectorStores, vectorStoreNotice } = await getAgentPlaygroundOptions()
  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 px-4 pt-5 sm:px-6 lg:px-8">
        <div><h1 className="text-[20px] font-semibold tracking-tight">Agent playground</h1><p className="mt-1 text-xs text-muted-foreground">Run a tool-using agent on the gateway and watch every step.</p></div>
        <Button nativeButton={false} variant="outline" size="sm" render={<Link href="/agents/runs" />}><ListTreeIcon /> Runs</Button>
      </div>
      {notice ? <Alert className="mx-4 w-auto shrink-0 sm:mx-6 lg:mx-8"><InfoIcon /><AlertTitle>Agent runs unavailable</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert> : null}
      <AgentPlayground options={options} vectorStores={vectorStores} vectorStoreNotice={vectorStoreNotice} />
    </div>
  )
}
