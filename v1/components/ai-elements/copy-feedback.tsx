"use client"

import { CircleCheckIcon, CopyIcon, type LucideIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import styles from "./copy-feedback.module.css"

export function CopyFeedbackIcon({ copied, className, idleIcon: IdleIcon = CopyIcon }: {
  copied: boolean
  className?: string
  idleIcon?: LucideIcon
}) {
  return (
    <span aria-hidden="true" data-slot="copy-feedback" data-copied={copied} className={cn("size-4", styles.icon, className)}>
      <IdleIcon className={styles.idle} />
      <CircleCheckIcon className={styles.success} />
    </span>
  )
}

/** Both labels share a cell so the icon stays still when the label changes. */
export function CopyFeedbackText({ copied, idleLabel = "Copy", className }: {
  copied: boolean
  idleLabel?: string
  className?: string
}) {
  return (
    <span aria-hidden="true" data-copied={copied} className={cn(styles.text, className)}>
      <span className={styles.textIdle}>{idleLabel}</span>
      <span className={styles.textSuccess}>Copied!</span>
    </span>
  )
}
