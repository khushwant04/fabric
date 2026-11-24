"use client"

import { useState } from "react"
import { RocketIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"
import { diffDocuments, type PolicyDocument, type ValidationIssue } from "@/lib/fabric/routing-policy"

const kinds = { added: "Added", removed: "Removed", changed: "Changed", moved: "Reordered" } as const

export function PublishDialog({
  live,
  liveVersion,
  draft,
  names,
  errors,
  warnings,
  disabled,
  pending,
  onPublish,
}: {
  live: PolicyDocument | null
  liveVersion: number | null
  draft: PolicyDocument
  names: Map<string, string>
  errors: ValidationIssue[]
  warnings: ValidationIssue[]
  disabled: boolean
  pending: boolean
  onPublish: (note: string) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState("")
  const diff = diffDocuments(live, draft, (id) => names.get(id) ?? id.slice(0, 8))
  return <Dialog open={open} onOpenChange={(next) => { if (!pending) setOpen(next) }}>
    <DialogTrigger render={<Button disabled={disabled}><RocketIcon /> Publish</Button>} />
    <DialogContent showCloseButton={!pending} className="sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>Publish version {(liveVersion ?? 0) + 1}?</DialogTitle>
        <DialogDescription>Stamps and the edge pick the new version up with their next configuration pull, usually within seconds, and keep enforcing it if the control plane becomes unreachable.</DialogDescription>
      </DialogHeader>
      <div className="flex max-h-72 flex-col gap-2 overflow-y-auto">
        <div className="text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase">{live ? `Changes from live v${liveVersion}` : "First version"}</div>
        {diff.length ? <ul className="flex flex-col divide-y divide-border rounded-md border border-border">{diff.map((entry, index) => <li key={index} className="flex gap-3 px-3 py-2 text-sm">
          <span className={cn("w-20 shrink-0 text-xs font-medium", entry.kind === "removed" ? "text-destructive" : entry.kind === "added" ? "text-status-good" : "text-muted-foreground")}>{kinds[entry.kind]}</span>
          <span className="min-w-0"><span className="font-medium">{entry.rule}</span><span className="block break-words text-xs text-muted-foreground">{entry.detail}</span></span>
        </li>)}</ul> : <p className="text-sm text-muted-foreground">The draft is identical to the live version.</p>}
        {errors.length ? <ul className="flex flex-col gap-1">{errors.map((issue, index) => <li key={index} className="rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">{issue.message}</li>)}</ul> : null}
        {warnings.length ? <ul className="flex flex-col gap-1">{warnings.map((issue, index) => <li key={index} className="rounded-lg bg-muted px-3 py-2 text-xs">{issue.message}</li>)}</ul> : null}
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="publish-note">Note</Label>
        <Input id="publish-note" placeholder="What changed and why (optional)" maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} disabled={pending} />
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" disabled={pending} onClick={() => setOpen(false)}>Cancel</Button>
        <Button type="button" disabled={pending || errors.length > 0 || (Boolean(live) && !diff.length)} onClick={async () => { if (await onPublish(note)) { setOpen(false); setNote("") } }}>{pending ? <Spinner /> : <RocketIcon />}{pending ? "Publishing…" : "Publish"}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}
