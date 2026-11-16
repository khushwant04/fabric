import { UsersIcon } from "lucide-react"

import { PageContainer, PageHeader } from "@/components/console/page-header"
import { AddMemberDrawer } from "@/components/console/resource-dialogs"
import { StatusBadge } from "@/components/console/status-badge"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { listMembers } from "@/lib/fabric/data"
import { initials, memberIdentity } from "@/lib/fabric/people"
import { getConsoleContext, hasScope } from "@/lib/fabric/session"
import { shortId } from "@/lib/format"

export default async function MembersPage() {
  const [context, members] = await Promise.all([getConsoleContext(), listMembers()])
  const canWrite = hasScope(context, "members:write")
  return <PageContainer><PageHeader title="Members" description="Manage the people authorized to operate resources in this account." actions={<AddMemberDrawer disabled={!canWrite} />} />
    <Card><CardHeader><CardTitle>Account members</CardTitle><CardDescription>Roles determine the scopes issued when a member starts a console session. Names and emails are what each person’s identity provider reported at their last sign-in.</CardDescription></CardHeader>{members.length ? <CardContent className="px-0"><Table className="min-w-[640px]"><TableHeader><TableRow><TableHead className="pl-6">Member</TableHead><TableHead>Role</TableHead><TableHead>Status</TableHead><TableHead className="pr-6">User ID</TableHead></TableRow></TableHeader><TableBody>{members.map((member) => {
      const person = memberIdentity(member, context.me.user)
      return <TableRow key={member.user_id}>
        <TableCell className="pl-6"><div className="flex min-w-0 items-center gap-3"><Avatar className="size-8"><AvatarFallback className="text-[11px] font-medium">{person.pending ? "?" : initials(person.name)}</AvatarFallback></Avatar><div className="min-w-0 max-w-80"><div className={person.pending ? "truncate text-muted-foreground italic" : "flex min-w-0 items-center gap-2 font-medium"}><span className="truncate">{person.name}</span>{person.self ? <Badge variant="outline" className="shrink-0 text-[10px]">You</Badge> : null}</div>{person.detail ? <div className="truncate text-xs text-muted-foreground">{person.detail}</div> : null}</div></div></TableCell>
        <TableCell><Badge variant="secondary" className="capitalize">{member.role}</Badge></TableCell>
        <TableCell><StatusBadge status={member.status} /></TableCell>
        <TableCell className="pr-6 font-mono text-xs text-muted-foreground" title={member.user_id}>{shortId(member.user_id)}</TableCell>
      </TableRow>
    })}</TableBody></Table></CardContent> : <Empty className="min-h-72 border-0"><EmptyHeader><EmptyMedia variant="icon"><UsersIcon /></EmptyMedia><EmptyTitle>No members</EmptyTitle><EmptyDescription>Add an Auth0 identity to this account.</EmptyDescription></EmptyHeader></Empty>}</Card>
  </PageContainer>
}
