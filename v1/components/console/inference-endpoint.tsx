"use client"

import { useState } from "react"
import { CheckIcon, CopyIcon } from "lucide-react"

import { Button } from "@/components/ui/button"

export function InferenceEndpoint({ url, modelAlias }: { url: string; modelAlias: string }) {
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState(false)
  return <div className="space-y-3 rounded-lg border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><code className="break-all text-sm">{url}</code><Button variant="outline" size="sm" type="button" onClick={async () => {
      try { await navigator.clipboard.writeText(url); setCopied(true); setError(false); window.setTimeout(() => setCopied(false), 1500) }
      catch { setError(true) }
    }}>{copied ? <CheckIcon /> : <CopyIcon />}{copied ? "Copied" : "Copy URL"}</Button></div>
    <p className="text-xs text-muted-foreground">OpenAI base URL · model <code>{modelAlias}</code> · use a Fabric API key exchanged for an inference token.</p>
    {error ? <p role="status" className="text-xs text-destructive">Copy is unavailable. Select the URL to copy it.</p> : null}
  </div>
}
