"use client"

import * as React from "react"
import { DownloadIcon, Maximize2Icon } from "lucide-react"
import { StreamdownContext, extractTableDataFromElement, tableDataToCSV, tableDataToTSV, type Components, type ExtraProps } from "streamdown"
import { toast } from "sonner"

import { CopyFeedbackIcon } from "./copy-feedback"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { useCopyFeedback } from "./use-copy-feedback"
import { cn } from "@/lib/utils"
import { MessageRenderAction, MessageRenderCard } from "./message-render-card"
import styles from "./message-table.module.css"

type TableProps = React.ComponentProps<"table"> & ExtraProps

function removeIds(children: React.ReactNode): React.ReactNode {
  return React.Children.map(children, (child) => {
    if (!React.isValidElement<{ id?: string; children?: React.ReactNode }>(child)) return child
    return React.cloneElement(child, { id: undefined }, removeIds(child.props.children))
  })
}

export function MessageMarkdownTable({ children, className, node: _node, id, ...props }: TableProps) {
  void _node
  const tableRef = React.useRef<HTMLTableElement>(null)
  const expandRef = React.useRef<HTMLButtonElement>(null)
  const { isAnimating, controls } = React.useContext(StreamdownContext)
  const { copied, copy } = useCopyFeedback()
  const [expanded, setExpanded] = React.useState(false)
  const tableControls = typeof controls === "object" ? controls.table : controls
  const config = typeof tableControls === "object" ? tableControls : undefined
  const enabled = controls !== false && tableControls !== false
  const showCopy = enabled && config?.copy !== false
  const showDownload = enabled && config?.download !== false
  const showExpand = enabled && config?.fullscreen !== false

  async function copyTable() {
    if (!tableRef.current || isAnimating) return
    try {
      if (!await copy(tableDataToTSV(extractTableDataFromElement(tableRef.current)))) toast.error("Unable to copy table")
    } catch { toast.error("Unable to copy table") }
  }
  function downloadTable() {
    if (!tableRef.current || isAnimating) return
    try {
      const content = tableDataToCSV(extractTableDataFromElement(tableRef.current), config?.csvSeparator)
      const url = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }))
      const link = document.createElement("a")
      link.href = url
      link.download = typeof config?.download === "object" ? config.download.filename : "table.csv"
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { toast.error("Unable to download table") }
  }

  return (
    <>
      <MessageRenderCard data-slot="message-table" title="Data table" actions={<>
            {showCopy && <MessageRenderAction label={copied ? "Copied!" : "Copy table"} disabled={isAnimating} onClick={copyTable}><CopyFeedbackIcon copied={copied} className="size-4" /><span className="sr-only" role="status">{copied ? "Copied to clipboard" : ""}</span></MessageRenderAction>}
            {showDownload && <MessageRenderAction label="Download CSV" disabled={isAnimating} onClick={downloadTable}><DownloadIcon className="size-4" /></MessageRenderAction>}
            {showExpand && <MessageRenderAction ref={expandRef} label="Expand table" onClick={() => setExpanded(true)}><Maximize2Icon className="size-4" /></MessageRenderAction>}
          </>}>
        <div data-table-scroll className={styles.scroll} tabIndex={0} role="region" aria-label="Data table contents">
          <table ref={tableRef} id={id} className={cn(styles.table, className)} {...props}>{children}</table>
        </div>
      </MessageRenderCard>
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent finalFocus={expandRef} className="flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[min(1200px,calc(100%-2rem))]">
          <div className="shrink-0 border-b border-border/40 px-4 py-4 pr-12"><DialogTitle className="text-sm">Data table</DialogTitle><DialogDescription className="sr-only">Expanded table. Scroll to view all columns and rows.</DialogDescription></div>
          <div data-table-scroll className={cn(styles.scroll, styles.fullscreen)} tabIndex={0} role="region" aria-label="Expanded data table contents"><table className={cn(styles.table, className)} {...props}>{removeIds(children)}</table></div>
        </DialogContent>
      </Dialog>
    </>
  )
}

function MessageTableHead({ node: _node, className, ...props }: React.ComponentProps<"thead"> & ExtraProps) { void _node; return <thead className={cn(styles.head, className)} {...props} /> }
function MessageTableBody({ node: _node, className, ...props }: React.ComponentProps<"tbody"> & ExtraProps) { void _node; return <tbody className={cn(styles.body, className)} {...props} /> }
function MessageTableRow({ node: _node, className, ...props }: React.ComponentProps<"tr"> & ExtraProps) { void _node; return <tr className={cn(styles.row, className)} {...props} /> }
function MessageTableHeaderCell({ node: _node, className, align, style, ...props }: React.ComponentProps<"th"> & ExtraProps) { void _node; return <th scope="col" align={align} className={cn(styles.cell, styles.headerCell, className)} style={{ ...style, textAlign: align && align !== "char" ? align : style?.textAlign }} {...props} /> }
function MessageTableCell({ node: _node, className, align, style, ...props }: React.ComponentProps<"td"> & ExtraProps) { void _node; return <td align={align} className={cn(styles.cell, className)} style={{ ...style, textAlign: align && align !== "char" ? align : style?.textAlign }} {...props} /> }

export const messageTableComponents = {
  table: MessageMarkdownTable,
  thead: MessageTableHead,
  tbody: MessageTableBody,
  tr: MessageTableRow,
  th: MessageTableHeaderCell,
  td: MessageTableCell,
} satisfies Components
