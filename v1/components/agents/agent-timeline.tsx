"use client"

import {
  BookOpenTextIcon, CalculatorIcon, CircleCheckIcon, CircleXIcon, ClockIcon, CodeXmlIcon, FileImageIcon,
  GlobeIcon, PlugIcon, SearchIcon, SparklesIcon, WrenchIcon,
} from "lucide-react"
import type { ComponentType } from "react"

import { Shimmer } from "@/components/ai-elements/shimmer"
import { Spinner } from "@/components/ui/spinner"
import type { AgentFile, Citation, ModelItem, ToolItem } from "@/lib/fabric/agent-stream"
import { cn } from "@/lib/utils"

const toolIcons: Record<string, ComponentType<{ className?: string }>> = {
  calculator: CalculatorIcon, current_time: ClockIcon, code_interpreter: CodeXmlIcon, web_fetch: GlobeIcon,
  web_search: SearchIcon, file_search: BookOpenTextIcon,
}

const detailLabels: Record<string, string> = {
  backend: "backend", exit_code: "exit", timed_out: "timed out", files: "files", file_bytes: "file bytes", http_status: "HTTP",
  fetched_bytes: "fetched bytes", text_chars: "characters", redirects: "redirects", results: "results", vector_stores: "stores", ranking: "ranking",
}

function ms(value: number | null) {
  if (value === null) return null
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`
}

function Rail({ icon: Icon, running, failed, children, last }: { icon: ComponentType<{ className?: string }>; running: boolean; failed?: boolean; children: React.ReactNode; last?: boolean }) {
  return <li className="relative pb-4 pl-10">
    {!last ? <span aria-hidden="true" className="absolute top-8 bottom-0 left-[13px] w-px bg-border" /> : null}
    <span aria-hidden="true" className={cn("absolute top-0.5 left-0 grid size-7 place-items-center rounded-lg border bg-card", failed && "border-destructive/30 text-destructive")}>
      {running ? <Spinner className="size-3.5" /> : <Icon className="size-3.5 text-muted-foreground" />}
    </span>
    {children}
  </li>
}

export function ModelStep({ item, last }: { item: ModelItem; last?: boolean }) {
  const running = item.status === "running"
  return <Rail icon={SparklesIcon} running={running} last={last}>
    <div className="flex min-h-7 flex-wrap items-center gap-x-3 gap-y-1 text-[13px]">
      <span className="font-medium">Model call</span>
      {running ? <Shimmer duration={1.5} className="text-[13px]">Thinking…</Shimmer> : <>
        <span className="font-mono text-[12px] text-muted-foreground">{item.model ?? "model"}</span>
        <span className="text-xs tabular-nums text-muted-foreground">{item.inputTokens.toLocaleString("en")} in · {item.outputTokens.toLocaleString("en")} out{ms(item.latencyMs) ? ` · ${ms(item.latencyMs)}` : ""}</span>
        {item.finishReason ? <span className="rounded-md border px-1.5 py-0.5 text-[11px] text-muted-foreground">{item.finishReason === "tool_calls" ? `${item.toolCalls} tool call${item.toolCalls === 1 ? "" : "s"}` : item.finishReason}</span> : null}
      </>}
    </div>
    {item.cascade?.path.length ? <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
      <span>Cascade</span>
      {item.cascade.path.map((alias, index) => <span key={`${alias}-${index}`} className="flex items-center gap-1.5">{index ? <span aria-hidden="true">›</span> : null}<span className={cn("rounded-md border px-1.5 py-0.5 font-mono", index === item.cascade!.path.length - 1 && "border-foreground/20 text-foreground")}>{alias}</span></span>)}
      <span>{item.cascade.escalations ? `· escalated: ${item.cascade.reasons.join(", ")}` : "· accepted first answer"}</span>
    </div> : null}
  </Rail>
}

export function AgentImage({ file }: { file: AgentFile }) {
  if (!file.dataUrl) {
    return <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-xs text-muted-foreground"><FileImageIcon className="size-3.5" />{file.name} · {file.bytes.toLocaleString("en")} bytes{file.omitted ? ` · not included (${file.omitted.replaceAll("_", " ")})` : ""}</div>
  }
  // eslint-disable-next-line @next/next/no-img-element -- a data URL produced by the run, not an optimisable asset
  return <figure className="overflow-hidden rounded-xl border bg-white"><img src={file.dataUrl} alt={`Produced by the run: ${file.name}`} className="max-h-96 w-full object-contain" /><figcaption className="border-t bg-card px-3 py-1.5 text-[11px] text-muted-foreground">{file.name} · {file.bytes.toLocaleString("en")} bytes</figcaption></figure>
}

export function CitationList({ citations, className }: { citations: Citation[]; className?: string }) {
  if (!citations.length) return null
  return <div className={cn("space-y-1.5", className)}>
    <div className="text-[11px] font-medium text-muted-foreground">Sources</div>
    <ol className="grid gap-1.5 sm:grid-cols-2">{citations.map((item, index) => <li key={item.citation}>
      <details className="group rounded-lg border bg-card px-3 py-2 text-xs">
        <summary className="flex cursor-pointer list-none items-center gap-2"><span className="grid size-4 shrink-0 place-items-center rounded bg-muted text-[10px] tabular-nums">{index + 1}</span><span className="truncate font-mono">{item.citation}</span>{item.score !== null ? <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">{item.score.toFixed(2)}</span> : null}</summary>
        <p className="mt-2 whitespace-pre-wrap leading-5 text-muted-foreground">{item.text}</p>
      </details>
    </li>)}</ol>
  </div>
}

export function ToolStep({ item, last }: { item: ToolItem; last?: boolean }) {
  const running = item.status === "running"
  const failed = item.status === "done" && item.ok === false
  const mcp = item.name.includes("__") && !toolIcons[item.name]
  const Icon = toolIcons[item.hostedTool ?? item.name] ?? (mcp ? PlugIcon : WrenchIcon)
  const isCode = (item.hostedTool ?? item.name) === "code_interpreter"
  let code: string | null = null
  if (isCode && item.arguments) {
    try { const parsed = JSON.parse(item.arguments.replace(/… \(\d[\d,]* more characters\)$/, "")); code = typeof parsed.code === "string" ? parsed.code : null } catch { code = null }
  }
  return <Rail icon={Icon} running={running} failed={failed} last={last}>
    <div className="flex min-h-7 flex-wrap items-center gap-x-3 gap-y-1 text-[13px]">
      <span className="font-mono text-[12.5px] font-medium">{item.name}</span>
      {running ? <span className="text-xs text-muted-foreground">Running…</span> : <>
        <span className={cn("inline-flex items-center gap-1 text-xs", failed ? "text-destructive" : "text-muted-foreground")}>{failed ? <CircleXIcon className="size-3.5" /> : <CircleCheckIcon className="size-3.5 text-status-good" />}{failed ? item.error ?? "failed" : "ok"}{ms(item.durationMs) ? ` · ${ms(item.durationMs)}` : ""}</span>
        {item.executor ? <span className="text-[11px] text-muted-foreground">{item.executor}</span> : null}
        {item.details.map(([key, value]) => <span key={key} className="rounded-md border px-1.5 py-0.5 text-[11px] text-muted-foreground">{detailLabels[key] ?? key} {value}</span>)}
        {item.truncated ? <span className="text-[11px] text-status-warning">result truncated</span> : null}
      </>}
    </div>
    {item.argumentsSummary && !code ? <p className="mt-1 truncate font-mono text-[12px] text-muted-foreground" title={item.argumentsSummary}>{item.argumentsSummary}</p> : null}
    {code ? <pre className="mt-2 max-h-56 overflow-auto rounded-lg border bg-muted/30 px-3 py-2 font-mono text-[12px] leading-5">{code}</pre> : null}
    {item.files.length ? <div className="mt-2 grid gap-2 sm:grid-cols-2">{item.files.map((file) => <AgentImage key={file.name} file={file} />)}</div> : null}
    {item.citations.length ? <CitationList citations={item.citations} className="mt-2" /> : null}
    {item.result || (item.arguments && !code) ? <details className="mt-2 rounded-lg border bg-muted/15 text-xs">
      <summary className="cursor-pointer px-3 py-1.5 text-muted-foreground">Arguments and result</summary>
      <div className="space-y-2 border-t px-3 py-2">
        {item.arguments && !code ? <div><div className="mb-1 text-[11px] text-muted-foreground">Arguments</div><pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11.5px]">{item.arguments}</pre></div> : null}
        {item.result ? <div><div className="mb-1 text-[11px] text-muted-foreground">Result{item.result.length >= 2000 ? " (truncated)" : ""}</div><pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all font-mono text-[11.5px]">{item.result}</pre></div> : null}
      </div>
    </details> : null}
  </Rail>
}
