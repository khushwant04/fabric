import { UsersIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { AddMemberDialog } from "@/components/console/resource-dialogs"
import { StatusBadge } from "@/components/console/status-badge"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listMembers } from "@/lib/fabric/data"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { shortId } from "@/lib/format"

export default async function MembersPage() {
  const [context, members] = await Promise.all([getConsoleContext(), listMembers()])
  const canWrite = hasScope(context, "members:write") && !context.demo
  return <PageContainer><PageHeader title="Members" description="Manage the people authorized to operate resources in this account." actions={<AddMemberDialog disabled={!canWrite} />} />
    <Card><CardHeader><CardTitle>Account members</CardTitle><CardDescription>Roles determine the scopes issued when a member starts a console session.</CardDescription></CardHeader>{members.length ? <CardContent className="px-0"><Table><TableHeader><TableRow><TableHead className="pl-6">Member</TableHead><TableHead>Role</TableHead><TableHead>Status</TableHead><TableHead className="pr-6">Account</TableHead></TableRow></TableHeader><TableBody>{members.map((member) => <TableRow key={member.user_id}><TableCell className="pl-6 font-medium">{member.user_id === context.me.user.id ? context.me.user.display_name || context.me.user.email || "Current user" : `User ${shortId(member.user_id)}`}<div className="font-mono text-[11px] font-normal text-muted-foreground">{shortId(member.user_id)}</div></TableCell><TableCell><Badge variant="secondary" className="capitalize">{member.role}</Badge></TableCell><TableCell><StatusBadge status={member.status} /></TableCell><TableCell className="pr-6 font-mono text-xs text-muted-foreground">{shortId(member.account_id)}</TableCell></TableRow>)}</TableBody></Table></CardContent> : <Empty className="min-h-72 border-0"><EmptyHeader><EmptyMedia variant="icon"><UsersIcon /></EmptyMedia><EmptyTitle>No members</EmptyTitle><EmptyDescription>Add an Auth0 identity to this account.</EmptyDescription></EmptyHeader></Empty>}</Card>
  </PageContainer>
}
