"use client"

import { AlertTriangleIcon, RotateCcwIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"

export default function ConsoleError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <PageContainer><PageHeader title="Something went wrong" description="Fabric could not load this console page." /><Alert variant="destructive"><AlertTriangleIcon /><AlertTitle>Control-plane request failed</AlertTitle><AlertDescription>{error.message || "An unexpected error occurred."}</AlertDescription></Alert><div><Button onClick={reset}><RotateCcwIcon /> Try again</Button></div></PageContainer>
}
