import { ArrowLeftIcon, ArrowRightIcon, DownloadIcon, FilterIcon, ScrollTextIcon, ShieldAlertIcon } from "lucide-react"
import Link from "next/link"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { Section, SectionFooter } from "@/components/console/section"
import { TruncatedText } from "@/components/console/truncated-text"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { AUDIT_ACTIONS, AUDIT_PAGE_SIZE, auditActionLabel, auditActorLabel, auditResourceLabel, auditHref, auditQuery, parseAuditSearch, summarizeAuditMetadata } from "@/lib/fabric/audit"
import { listAuditEvents } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { formatDate, shortId } from "@/lib/format"

export default async function AuditPage({ searchParams }: PageProps<"/audit">) {
  const context = await getConsoleContext()
  const header = <PageHeader title="Audit log" description="Every privileged change in this account, recorded in the same transaction as the change itself." />
  if (!hasScope(context, "audit:read")) {
    return <PageContainer>{header}<Section title="Events"><Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><ShieldAlertIcon /></EmptyMedia><EmptyTitle>Owners and admins only</EmptyTitle><EmptyDescription>The audit trail names members, principals and the changes they made, so reading it needs the audit:read scope.</EmptyDescription></EmptyHeader></Empty></Section></PageContainer>
  }

  const search = parseAuditSearch(await searchParams)
  const page = await listAuditEvents(auditQuery(search, { limit: String(AUDIT_PAGE_SIZE) }))
  const filtered = Boolean(search.action || search.resource)
  const exportHref = auditHref("/api/audit/export", { action: search.action, resource: search.resource })

  return <PageContainer>
    <PageHeader title="Audit log" description="Every privileged change in this account, recorded in the same transaction as the change itself." actions={<Button nativeButton={false} variant="outline" render={<a href={exportHref} download />}><DownloadIcon /> Export NDJSON</Button>} />
    <Section title="Events" description="Newest first. Exports are recorded in this log too.">
      <form className="flex flex-col gap-2 px-6 pb-5 sm:flex-row sm:items-center" action="/audit">
      <NativeSelect name="action" defaultValue={search.action ?? ""} aria-label="Action" className="sm:w-64">
        <NativeSelectOption value="">All actions</NativeSelectOption>
        {Object.entries(AUDIT_ACTIONS).map(([action, label]) => <NativeSelectOption key={action} value={action}>{label}</NativeSelectOption>)}
      </NativeSelect>
      <Input name="resource" defaultValue={search.resource ?? ""} placeholder="Resource ID" aria-label="Resource ID" className="sm:max-w-xs" maxLength={128} />
      <Button type="submit" variant="outline"><FilterIcon /> Filter</Button>
      {filtered ? <Button nativeButton={false} variant="ghost" render={<Link href="/audit" />}>Clear</Button> : null}
      </form>
      {page.events.length ? <Table className="min-w-[900px]"><TableHeader><TableRow><TableHead>Time</TableHead><TableHead>Action</TableHead><TableHead>Actor</TableHead><TableHead>Resource</TableHead><TableHead className="w-full">Details</TableHead></TableRow></TableHeader><TableBody>
        {page.events.map((event) => <TableRow key={event.id}>
          <TableCell className="whitespace-nowrap text-muted-foreground tabular-nums"><time dateTime={event.created_at}>{formatDate(event.created_at)}</time></TableCell>
          <TableCell><Link className="block max-w-56 whitespace-nowrap font-medium hover:underline" href={auditHref("/audit", { action: event.action })}><TruncatedText text={auditActionLabel(event.action)} /></Link></TableCell>
          <TableCell><div className="whitespace-nowrap">{auditActorLabel(event.actor_type)}</div>{event.actor_id ? <div className="font-mono text-[11px] text-muted-foreground" title={event.actor_id}>{shortId(event.actor_id)}</div> : null}</TableCell>
          <TableCell>{event.resource_id ? <Link className="group/resource block w-fit" href={auditHref("/audit", { resource: event.resource_id })} title={`Show every event for ${event.resource_id}`}><span className="block whitespace-nowrap group-hover/resource:underline">{auditResourceLabel(event.resource_type ?? "")}</span><span className="block font-mono text-[11px] text-muted-foreground">{shortId(event.resource_id)}</span></Link> : <span className="text-muted-foreground">{event.resource_type ? auditResourceLabel(event.resource_type) : "—"}</span>}</TableCell>
          <TableCell className="w-full max-w-0 text-xs text-muted-foreground">{summarizeAuditMetadata(event.metadata) ? <TruncatedText text={summarizeAuditMetadata(event.metadata)} /> : "—"}</TableCell>
        </TableRow>)}
      </TableBody></Table> : <Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><ScrollTextIcon /></EmptyMedia><EmptyTitle>No matching events</EmptyTitle><EmptyDescription>{filtered ? "Nothing in this account's trail matches the filter." : "Changes to this account will appear here."}</EmptyDescription></EmptyHeader></Empty>}
      {search.cursor || page.next_cursor ? <SectionFooter>
        {search.cursor ? <Button nativeButton={false} variant="outline" render={<Link href={auditHref("/audit", { action: search.action, resource: search.resource })} />}><ArrowLeftIcon /> Newest</Button> : <span />}
        {page.next_cursor ? <Button nativeButton={false} variant="outline" render={<Link href={auditHref("/audit", { ...search, cursor: page.next_cursor })} />}>Older <ArrowRightIcon /></Button> : null}
      </SectionFooter> : null}
    </Section>
  </PageContainer>
}
