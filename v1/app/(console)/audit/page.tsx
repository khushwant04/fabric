import { ArrowLeftIcon, ArrowRightIcon, DownloadIcon, FilterIcon, ScrollTextIcon, ShieldAlertIcon } from "lucide-react"
import Link from "next/link"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { AUDIT_ACTIONS, AUDIT_PAGE_SIZE, auditActionLabel, auditHref, auditQuery, parseAuditSearch, summarizeAuditMetadata } from "@/lib/fabric/audit"
import { listAuditEvents } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate, shortId } from "@/lib/format"

export default async function AuditPage({ searchParams }: PageProps<"/audit">) {
  const context = await getConsoleContext()
  const header = <PageHeader title="Audit log" description="Every privileged change in this account, recorded in the same transaction as the change itself." />
  if (!hasScope(context, "audit:read")) {
    return <PageContainer>{header}<Card><Empty className="min-h-72 border-0"><EmptyHeader><EmptyMedia variant="icon"><ShieldAlertIcon /></EmptyMedia><EmptyTitle>Owners and admins only</EmptyTitle><EmptyDescription>The audit trail names members, principals and the changes they made, so reading it needs the audit:read scope.</EmptyDescription></EmptyHeader></Empty></Card></PageContainer>
  }

  const search = parseAuditSearch(await searchParams)
  const page = await listAuditEvents(auditQuery(search, { limit: String(AUDIT_PAGE_SIZE) }))
  const filtered = Boolean(search.action || search.resource)
  const exportHref = auditHref("/api/audit/export", { action: search.action, resource: search.resource })

  return <PageContainer>
    <PageHeader title="Audit log" description="Every privileged change in this account, recorded in the same transaction as the change itself." actions={<Button nativeButton={false} variant="outline" render={<a href={exportHref} download />}><DownloadIcon /> Export NDJSON</Button>} />
    <Card><CardContent><form className="flex flex-col gap-2 sm:flex-row" action="/audit">
      <NativeSelect name="action" defaultValue={search.action ?? ""} aria-label="Action" className="sm:w-64">
        <NativeSelectOption value="">All actions</NativeSelectOption>
        {Object.entries(AUDIT_ACTIONS).map(([action, label]) => <NativeSelectOption key={action} value={action}>{label}</NativeSelectOption>)}
      </NativeSelect>
      <Input name="resource" defaultValue={search.resource ?? ""} placeholder="Resource ID" aria-label="Resource ID" className="sm:max-w-xs" maxLength={128} />
      <Button type="submit" variant="outline"><FilterIcon /> Filter</Button>
      {filtered ? <Button nativeButton={false} variant="ghost" render={<Link href="/audit" />}>Clear</Button> : null}
    </form></CardContent></Card>
    <Card><CardHeader><CardTitle>Events</CardTitle><CardDescription>Newest first. Exports are recorded in this log too.</CardDescription></CardHeader>
      {page.events.length ? <CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Time</TableHead><TableHead>Action</TableHead><TableHead>Actor</TableHead><TableHead>Resource</TableHead><TableHead className="pr-6">Details</TableHead></TableRow></TableHeader><TableBody>
        {page.events.map((event) => <TableRow key={event.id}>
          <TableCell className="pl-6 whitespace-nowrap text-muted-foreground">{formatDate(event.created_at)}</TableCell>
          <TableCell><Link className="font-medium hover:underline" href={auditHref("/audit", { action: event.action })}>{auditActionLabel(event.action)}</Link></TableCell>
          <TableCell><Badge variant="outline">{event.actor_type}</Badge> <span className="font-mono text-xs text-muted-foreground">{event.actor_id ? shortId(event.actor_id) : "system"}</span></TableCell>
          <TableCell>{event.resource_id ? <Link className="font-mono text-xs hover:underline" href={auditHref("/audit", { resource: event.resource_id })}>{event.resource_type} {shortId(event.resource_id)}</Link> : <span className="text-muted-foreground">—</span>}</TableCell>
          <TableCell className="max-w-md truncate pr-6 text-xs text-muted-foreground" title={summarizeAuditMetadata(event.metadata)}>{summarizeAuditMetadata(event.metadata) || "—"}</TableCell>
        </TableRow>)}
      </TableBody></Table></CardContent> : <Empty className="min-h-72 border-0"><EmptyHeader><EmptyMedia variant="icon"><ScrollTextIcon /></EmptyMedia><EmptyTitle>No matching events</EmptyTitle><EmptyDescription>{filtered ? "Nothing in this account's trail matches the filter." : "Changes to this account will appear here."}</EmptyDescription></EmptyHeader></Empty>}
      {search.cursor || page.next_cursor ? <CardContent className="flex justify-between gap-2">
        {search.cursor ? <Button nativeButton={false} variant="outline" render={<Link href={auditHref("/audit", { action: search.action, resource: search.resource })} />}><ArrowLeftIcon /> Newest</Button> : <span />}
        {page.next_cursor ? <Button nativeButton={false} variant="outline" render={<Link href={auditHref("/audit", { ...search, cursor: page.next_cursor })} />}>Older <ArrowRightIcon /></Button> : null}
      </CardContent> : null}
    </Card>
  </PageContainer>
}
