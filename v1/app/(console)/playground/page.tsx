import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { PageContainer, PageHeader } from "@/components/console/page-header"
import { LiveRefresh } from "@/components/console/live-refresh"
import { Playground } from "@/components/playground/playground"
import { getPlaygroundOptions } from "@/lib/fabric/playground-data"
import { InfoIcon } from "lucide-react"

export default async function PlaygroundPage() {
  const { options, notice } = await getPlaygroundOptions()
  return (
    <PageContainer>
      <PageHeader title="Inference playground" description="Send the same prompt to two model deployments and compare their live responses." />
      <LiveRefresh checkedAt={new Date().toISOString()} />
      {notice ? <Alert><InfoIcon /><AlertTitle>Inference unavailable</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert> : null}
      <Playground options={options} />
    </PageContainer>
  )
}
