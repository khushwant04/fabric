import { PageContainer } from "@/components/console/page-header"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

export default function ConsoleLoading() {
  return <PageContainer><div className="space-y-3"><Skeleton className="h-8 w-52" /><Skeleton className="h-4 w-[min(34rem,80%)]" /><Skeleton className="h-px w-full" /></div><div className="grid gap-5 md:grid-cols-3">{Array.from({ length: 3 }).map((_, index) => <Card key={index}><CardHeader><Skeleton className="h-4 w-28" /><Skeleton className="h-7 w-16" /></CardHeader></Card>)}</div><Card><CardHeader><Skeleton className="h-5 w-44" /><Skeleton className="h-4 w-72" /></CardHeader><CardContent>{Array.from({ length: 6 }).map((_, index) => <Skeleton className="mb-3 h-10 w-full" key={index} />)}</CardContent></Card></PageContainer>
}
