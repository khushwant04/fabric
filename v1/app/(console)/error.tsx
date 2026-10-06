"use client"

import { AlertTriangleIcon, RotateCcwIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"

export default function ConsoleError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <PageContainer>
      <PageHeader title="Unable to load this page" description="Fabric could not retrieve the information for this page." />
      <Alert variant="destructive">
        <AlertTriangleIcon />
        <AlertTitle>Page temporarily unavailable</AlertTitle>
        <AlertDescription>
          <p>Try again. If the problem continues, check the console and control-plane server logs.</p>
          {error.digest ? <p className="mt-2 font-mono text-xs">Error reference: {error.digest}</p> : null}
        </AlertDescription>
      </Alert>
      <div><Button onClick={retry}><RotateCcwIcon /> Try again</Button></div>
    </PageContainer>
  )
}
