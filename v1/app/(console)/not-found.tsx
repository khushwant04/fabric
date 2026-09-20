import Link from "next/link"
import { ArrowLeftIcon, SearchXIcon } from "lucide-react"

import { PageContainer } from "@/components/console/page-header"
import { Button } from "@/components/ui/button"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"

export default function NotFound() {
  return <PageContainer><Empty className="min-h-[60vh]"><EmptyHeader><EmptyMedia variant="icon"><SearchXIcon /></EmptyMedia><EmptyTitle>Resource not found</EmptyTitle><EmptyDescription>The resource may have been deleted or does not belong to this account.</EmptyDescription></EmptyHeader><EmptyContent><Button variant="outline" render={<Link href="/dashboard" />}><ArrowLeftIcon /> Return to overview</Button></EmptyContent></Empty></PageContainer>
}
