import type { ReactNode } from "react"

import { ConsoleBreadcrumbs } from "@/components/console/breadcrumbs"

export function PageHeader({
  title,
  description,
  actions,
  children,
}: {
  title: string
  description?: string
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    // A 26px title over one muted line, actions on the right. Nested pages show the path
    // above the title, since there is no top bar to carry it.
    <div className="flex flex-col gap-4">
      <ConsoleBreadcrumbs />
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-[26px] leading-8 font-medium tracking-[-0.025em]">{title}</h1>
          {description ? <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{description}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </div>
  )
}

export function PageContainer({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto flex w-full max-w-[1280px] flex-col gap-5 px-5 py-8 md:px-10 md:py-10">
      {children}
    </div>
  )
}
