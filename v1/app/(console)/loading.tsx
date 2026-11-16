import { PageContainer } from "@/components/console/page-header"
import { Skeleton } from "@/components/ui/skeleton"

export default function ConsoleLoading() {
  return <PageContainer>
    <div className="space-y-2"><Skeleton className="h-8 w-52" /><Skeleton className="h-4 w-[min(34rem,80%)]" /></div>
    <div className="grid gap-px border border-border bg-border md:grid-cols-3">{Array.from({ length: 3 }).map((_, index) => <div key={index} className="flex flex-col gap-3 bg-card p-4"><Skeleton className="h-3 w-24" /><Skeleton className="h-8 w-16" /><Skeleton className="h-3 w-32" /></div>)}</div>
    <section className="border border-border bg-card"><header className="flex h-10 items-center border-b border-border px-4"><Skeleton className="h-3 w-32" /></header><div className="flex flex-col">{Array.from({ length: 6 }).map((_, index) => <div className="flex h-11 items-center gap-4 border-b border-border px-4 last:border-b-0" key={index}><Skeleton className="h-3 w-40" /><Skeleton className="h-3 w-24" /><Skeleton className="ml-auto h-3 w-16" /></div>)}</div></section>
  </PageContainer>
}
