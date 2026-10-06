"use client"

import type { ComponentProps, ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import styles from "./message-render-card.module.css"

export function MessageRenderCard({ title, actions, className, children, ...props }: Omit<ComponentProps<"div">, "title"> & { title: ReactNode; actions?: ReactNode }) {
  return <div className={cn(styles.frame, className)} {...props}>
    <div data-slot="message-render-toolbar" className={styles.toolbar}>
      <span className={styles.label}>{title}</span>
      <div className={styles.actions}>{actions}</div>
    </div>
    {children}
  </div>
}

export function MessageRenderAction({ label, children, className, ...props }: ComponentProps<typeof Button> & { label: string }) {
  return <Tooltip><TooltipTrigger render={<Button variant="ghost" size="icon" aria-label={label} className={cn(styles.action, className)} {...props} />}>{children}</TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>
}
