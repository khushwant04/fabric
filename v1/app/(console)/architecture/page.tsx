import { PageContainer, PageHeader } from "@/components/console/page-header"
import { ArchitectureExplorer } from "@/components/architecture/explorer"

export default function ArchitecturePage() {
  return (
    <PageContainer>
      <PageHeader title="How Fabric works" description="Follow a model from deployment intent to a running GPU workload, then trace an inference request." />
      <ArchitectureExplorer />
    </PageContainer>
  )
}
