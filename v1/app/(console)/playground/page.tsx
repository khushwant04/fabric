import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { LiveRefresh } from "@/components/console/live-refresh"
import { Playground } from "@/components/playground/playground"
import { getPlaygroundOptions } from "@/lib/fabric/playground-data"
import { InfoIcon } from "lucide-react"

export default async function PlaygroundPage() {
  const { options, notice } = await getPlaygroundOptions()
  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 px-4 pt-5 sm:px-6 lg:px-8">
        <div><h1 className="text-[20px] font-semibold tracking-tight">Playground</h1><p className="mt-1 text-xs text-muted-foreground">Explore your models, side by side.</p></div>
        <LiveRefresh checkedAt={new Date().toISOString()} />
      </div>
      {notice ? <Alert className="mx-4 w-auto shrink-0 sm:mx-6 lg:mx-8"><InfoIcon /><AlertTitle>Inference unavailable</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert> : null}
      <Playground options={options} />
    </div>
  )
}
