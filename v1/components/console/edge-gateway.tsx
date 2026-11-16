"use client"

import { useState } from "react"
import { CheckIcon, CopyIcon, WaypointsIcon } from "lucide-react"

import { Panel, PanelAction, PanelContent, PanelFooter, PanelHeader, PanelRow, PanelTitle } from "@/components/hexel/panel"
import { Status } from "@/components/hexel/status"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

// The edge gateway (ADR 0021) is one public inference URL in front of every stamp. These
// cards appear only when the console is configured with it (FABRIC_EDGE_INFERENCE_URL).

function GatewayUrl({ url }: { url: string }) {
  const [copied, setCopied] = useState(false)
  const [failed, setFailed] = useState(false)
  return <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3">
    <span aria-hidden="true" className="flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground"><WaypointsIcon className="size-4" /></span>
    <code className="min-w-0 flex-1 text-sm break-all">{url}</code>
    <Button variant="outline" size="sm" type="button" onClick={async () => {
      try { await navigator.clipboard.writeText(url); setCopied(true); setFailed(false); window.setTimeout(() => setCopied(false), 1500) }
      catch { setFailed(true) }
    }}>{copied ? <CheckIcon /> : <CopyIcon />}{copied ? "Copied" : "Copy URL"}</Button>
    <span role="status" className="sr-only">{copied ? "Edge gateway URL copied" : ""}</span>
    {failed ? <p className="w-full text-xs text-destructive">Copy is unavailable. Select the URL to copy it.</p> : null}
  </div>
}

const explanation = "One OpenAI base URL for every stamp. The edge checks the inference token and sends each request to a stamp that serves the requested model for your account."

/** Overview: a Hexel panel, like the rest of the page. */
export function EdgeGatewayPanel({ url, usedByConsole }: { url: string; usedByConsole: boolean }) {
  return <Panel>
    <PanelHeader><PanelTitle>Edge gateway</PanelTitle><PanelAction>{usedByConsole ? <Status tone="good">Playground routes through it</Status> : <Status>Configured</Status>}</PanelAction></PanelHeader>
    <PanelContent><PanelRow><GatewayUrl url={url} /></PanelRow></PanelContent>
    <PanelFooter>{explanation}</PanelFooter>
  </Panel>
}

/** Stamps: a card above the stamp table. */
export function EdgeGatewayCard({ url, usedByConsole, stamps }: { url: string; usedByConsole: boolean; stamps: number | null }) {
  return <Card>
    <CardHeader>
      <CardTitle>Edge gateway</CardTitle>
      <CardDescription>{explanation}{stamps !== null ? ` It routes to stamps that report an HTTPS inference URL${stamps ? `: ${stamps} of yours do` : ""}.` : ""}</CardDescription>
      <CardAction>{usedByConsole ? <Status tone="good">Playground routes through it</Status> : <Status>Configured</Status>}</CardAction>
    </CardHeader>
    <CardContent><GatewayUrl url={url} /></CardContent>
  </Card>
}
