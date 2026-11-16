"use client"

import Link from "next/link"
import { useEffect, useRef, useState } from "react"
import {
  ArrowUpIcon, BookOpenTextIcon, BotIcon, CheckIcon, ChevronDownIcon, CircleAlertIcon, ExternalLinkIcon,
  GitBranchIcon, PlugIcon, PlusIcon, RotateCcwIcon, ShieldAlertIcon, ShieldCheckIcon, SparklesIcon, SquareIcon,
  Trash2Icon, WorkflowIcon, WrenchIcon, XIcon,
} from "lucide-react"

import { AgentImage, CitationList, ModelStep, ToolStep } from "@/components/agents/agent-timeline"
import { Conversation, ConversationContent, ConversationScrollButton } from "@/components/ai-elements/conversation"
import { Message, MessageContent, MessageResponse } from "@/components/ai-elements/message"
import {
  ModelSelector, ModelSelectorContent, ModelSelectorEmpty, ModelSelectorGroup, ModelSelectorInput,
  ModelSelectorItem, ModelSelectorList, ModelSelectorName, ModelSelectorTrigger,
} from "@/components/ai-elements/model-selector"
import { Shimmer } from "@/components/ai-elements/shimmer"
import { Suggestion } from "@/components/ai-elements/suggestion"
import { Status } from "@/components/hexel/status"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import type { AgentModelOption, VectorStoreOption } from "@/lib/fabric/agent-data"
import {
  AGENT_MAX_CLIENT_TOOLS, AGENT_MAX_MCP_SERVERS, AGENT_MAX_STEPS, AGENT_MAX_TASK_CHARS, AGENT_TOOLS, type BuiltinTool,
} from "@/lib/fabric/agent-playground"
import { AgentStreamParser, clientAnswerItems, emptyRunView, reduceAgentEvent, resumeMessages, summariseArguments, type AgentRunView } from "@/lib/fabric/agent-stream"
import { cn } from "@/lib/utils"

const starters = [
  { title: "Split a bill", tools: ["calculator", "current_time"] as BuiltinTool[], task: "Split a 86.40 EUR dinner bill three ways with a 17.5% tip, and tell me what time it is in Tokyo right now." },
  { title: "Plot a curve", tools: ["code_interpreter"] as BuiltinTool[], task: "Plot y = sin(x) * exp(-x / 5) for x from 0 to 20 and describe where it crosses zero." },
  { title: "Research a topic", tools: ["web_search", "web_fetch"] as BuiltinTool[], task: "Find the latest stable release of PostgreSQL and summarise its headline features." },
]

type ClientToolDraft = { name: string; description: string; parameters: string }
type McpDraft = { label: string; url: string; allowedTools: string; requireApproval: "never" | "always"; authorization: string }

type RunConfig = {
  deploymentId: string
  model: AgentModelOption["model"]
  tools: BuiltinTool[]
  clientTools: Array<{ name: string; description: string; parameters: Record<string, unknown> | null }>
  vectorStoreIds: string[]
  mcpServers: Array<{ label: string; url: string; allowedTools: string[] | null; requireApproval: "never" | "always"; authorization: string | null }>
  maxSteps: number
  budgetTokens: number | null
}

const statusLabels: Record<AgentRunView["status"], { label: string; tone: "good" | "info" | "warning" | "critical" | "neutral" }> = {
  starting: { label: "Starting", tone: "info" },
  running: { label: "Running", tone: "info" },
  completed: { label: "Completed", tone: "good" },
  requires_action: { label: "Waiting for you", tone: "warning" },
  max_steps_reached: { label: "Step limit reached", tone: "warning" },
  failed: { label: "Failed", tone: "critical" },
  stopped: { label: "Stopped", tone: "neutral" },
}

function SectionTitle({ icon: Icon, children, action }: { icon: React.ComponentType<{ className?: string }>; children: React.ReactNode; action?: React.ReactNode }) {
  return <div className="flex items-center gap-2 px-1 text-[12px] font-medium text-muted-foreground"><Icon className="size-3.5" />{children}{action ? <span className="ml-auto">{action}</span> : null}</div>
}

function ModelPicker({ options, selected, disabled, onSelect }: { options: AgentModelOption[]; selected?: AgentModelOption; disabled: boolean; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const routing = options.filter((item) => item.model !== "exact")
  const exact = options.filter((item) => item.model === "exact")
  return <ModelSelector open={open} onOpenChange={setOpen}>
    <ModelSelectorTrigger render={<Button variant="outline" disabled={disabled || !options.length} className="h-auto w-full justify-start gap-3 rounded-xl px-3 py-2 text-left font-normal" />} aria-label="Choose model">
      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border bg-muted/40">{selected?.model === "cascade" ? <GitBranchIcon className="size-4 text-muted-foreground" /> : selected?.model === "auto" ? <SparklesIcon className="size-4 text-muted-foreground" /> : <BotIcon className="size-4 text-muted-foreground" />}</span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5"><span className="truncate font-mono text-[13px] font-medium">{selected?.label ?? "No model available"}</span><span className="truncate text-[11px] text-muted-foreground">{selected?.description ?? "Waiting for a ready deployment"}</span></span>
      <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground" />
    </ModelSelectorTrigger>
    <ModelSelectorContent title="Choose model" className="sm:max-w-md">
      <ModelSelectorInput placeholder="Search models…" />
      <ModelSelectorList><ModelSelectorEmpty>No matching model.</ModelSelectorEmpty>
        {[{ heading: "Gateway routing", items: routing }, { heading: "Deployments", items: exact }].filter((group) => group.items.length).map((group) => <ModelSelectorGroup key={group.heading} heading={group.heading}>
          {group.items.map((item) => <ModelSelectorItem key={item.id} value={`${item.id} ${item.label} ${item.description}`} onSelect={() => { onSelect(item.id); setOpen(false) }} className="gap-3 py-2.5">
            <ModelSelectorName><span className="block font-mono text-sm font-medium">{item.label}</span><span className="block truncate text-xs text-muted-foreground">{item.description}</span></ModelSelectorName>
            {item.id === selected?.id ? <CheckIcon className="size-4" /> : null}
          </ModelSelectorItem>)}
        </ModelSelectorGroup>)}
      </ModelSelectorList>
    </ModelSelectorContent>
  </ModelSelector>
}

/** Elapsed time across a run and its resumes, ticking while a request streams. */
function startClock(base: number, tick: (elapsedMs: number) => void) {
  const start = performance.now()
  const timer = setInterval(() => tick(base + performance.now() - start), 200)
  return { stop: () => { clearInterval(timer); return base + performance.now() - start } }
}

function parseParameters(text: string, name: string) {
  if (!text.trim()) return null
  let value: unknown
  try { value = JSON.parse(text) } catch { throw new Error(`Parameters for ${name || "a client tool"} are not valid JSON.`) }
  if (!value || typeof value !== "object" || Array.isArray(value) || (value as { type?: unknown }).type !== "object") throw new Error(`Parameters for ${name || "a client tool"} must be a JSON schema with "type": "object".`)
  return value as Record<string, unknown>
}

export function AgentPlayground({ options, vectorStores, vectorStoreNotice }: { options: AgentModelOption[]; vectorStores: VectorStoreOption[]; vectorStoreNotice: string | null }) {
  const [modelId, setModelId] = useState(options.find((item) => item.model === "exact")?.id ?? options[0]?.id ?? "")
  const [task, setTask] = useState("")
  const [tools, setTools] = useState<BuiltinTool[]>(["calculator", "current_time"])
  const [disabled, setDisabled] = useState<Partial<Record<BuiltinTool | "file_search" | "mcp", string>>>({})
  const [fileSearch, setFileSearch] = useState(false)
  const [storeIds, setStoreIds] = useState<string[]>([])
  const [clientTools, setClientTools] = useState<ClientToolDraft[]>([])
  const [mcpServers, setMcpServers] = useState<McpDraft[]>([])
  const [maxSteps, setMaxSteps] = useState(8)
  const [budget, setBudget] = useState("")
  const [view, setView] = useState<AgentRunView | null>(null)
  const [running, setRunning] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [submittedTask, setSubmittedTask] = useState("")
  const [cascadeHeaders, setCascadeHeaders] = useState<Record<string, string>>({})
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [decisions, setDecisions] = useState<Record<string, { approve: boolean; reason: string }>>({})
  const [elapsedMs, setElapsedMs] = useState(0)
  const controller = useRef<AbortController | null>(null)
  const config = useRef<RunConfig | null>(null)
  const viewRef = useRef<AgentRunView | null>(null)
  const mounted = useRef(true)
  const selected = options.find((item) => item.id === modelId) ?? options[0]

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; controller.current?.abort() }
  }, [])

  function apply(next: AgentRunView) {
    viewRef.current = next
    if (mounted.current) setView(next)
  }

  function buildConfig(): RunConfig {
    if (!selected) throw new Error("Choose a model.")
    const budgetTokens = budget.trim() ? Number(budget) : null
    if (budgetTokens !== null && (!Number.isInteger(budgetTokens) || budgetTokens < 1)) throw new Error("The token budget must be a positive whole number.")
    if (!Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > AGENT_MAX_STEPS) throw new Error(`Max steps must be between 1 and ${AGENT_MAX_STEPS}.`)
    if (fileSearch && !storeIds.length) throw new Error("Choose at least one vector store for file search.")
    return {
      deploymentId: selected.deploymentId, model: selected.model,
      tools: tools.filter((tool) => !disabled[tool]),
      clientTools: clientTools.map((tool) => ({ name: tool.name.trim(), description: tool.description.trim(), parameters: parseParameters(tool.parameters, tool.name.trim()) })),
      vectorStoreIds: fileSearch ? storeIds : [],
      mcpServers: mcpServers.map((server) => ({
        label: server.label.trim(), url: server.url.trim(), requireApproval: server.requireApproval, authorization: server.authorization.trim() || null,
        allowedTools: server.allowedTools.split(",").map((name) => name.trim()).filter(Boolean).length ? server.allowedTools.split(",").map((name) => name.trim()).filter(Boolean) : null,
      })),
      maxSteps, budgetTokens,
    }
  }

  async function execute(body: Record<string, unknown>, initial: AgentRunView, paused?: AgentRunView) {
    const abort = new AbortController()
    controller.current = abort
    setRunning(true)
    setFormError(null)
    apply(initial)
    const clock = startClock(elapsedMs, (value) => { if (mounted.current) setElapsedMs(value) })
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
    let current = initial
    let nothingRan = false
    try {
      const response = await fetch("/api/agents/runs", { method: "POST", headers: { "Content-Type": "application/json" }, signal: abort.signal, body: JSON.stringify(body) })
      if (!response.ok) {
        let message = `The agent run failed (${response.status}).`
        let payload: { error?: unknown; code?: unknown; tool?: unknown } = {}
        try { payload = await response.json() } catch { /* Keep a safe failure message. */ }
        if (typeof payload.error === "string") message = payload.error
        // A stamp that refuses a tool: switch it off and say why, so the next run works.
        if (payload.code === "hosted_tool_disabled" && typeof payload.tool === "string") {
          const tool = payload.tool as BuiltinTool
          setDisabled((value) => ({ ...value, [tool]: message }))
          setTools((value) => value.filter((item) => item !== tool))
        }
        if (payload.code === "file_search_disabled") { setDisabled((value) => ({ ...value, file_search: message })); setFileSearch(false) }
        if (payload.code === "mcp_disabled") setDisabled((value) => ({ ...value, mcp: message }))
        throw new Error(message)
      }
      if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) throw new Error("The agent run did not return a stream.")
      const headers: Record<string, string> = {}
      for (const name of ["X-Fabric-Cascade-Path", "X-Fabric-Cascade-Escalations", "X-Fabric-Cascade-Reason"]) { const value = response.headers.get(name); if (value) headers[name] = value }
      setCascadeHeaders(headers)
      const parser = new AgentStreamParser()
      const decoder = new TextDecoder()
      reader = response.body.getReader()
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        for (const event of parser.feed(decoder.decode(chunk.value, { stream: true }))) current = reduceAgentEvent(current, event)
        apply(current)
      }
      if (current.status === "running" || current.status === "starting") current = { ...current, status: "failed", error: current.error ?? "The stream ended before the run completed." }
    } catch (error) {
      const message = error instanceof Error ? error.message : "The agent run was interrupted."
      if (abort.signal.aborted) {
        current = { ...current, status: "stopped", items: current.items.map((item) => item.status === "running" ? { ...item, status: "done" } as typeof item : item) }
      } else if (!current.runId && !initial.items.length) {
        // Nothing ran: show the reason above the composer and keep the task editable.
        nothingRan = true
        setFormError(message)
      } else if (paused && current === initial) {
        // A resume was refused before it started: the run is still paused where it was.
        current = { ...paused, error: message }
      } else {
        current = { ...current, status: "failed", error: message }
      }
    } finally {
      const elapsed = clock.stop()
      if (nothingRan) { viewRef.current = null; if (mounted.current) setView(null) }
      else apply(current)
      if (mounted.current) { setRunning(false); setElapsedMs(elapsed) }
      try { await reader?.cancel() } catch { /* Already closed. */ }
    }
    return !nothingRan
  }

  async function start(text = task) {
    if (running || !text.trim()) return
    let next: RunConfig
    try { next = buildConfig() } catch (error) { setFormError(error instanceof Error ? error.message : "Check the run settings."); return }
    config.current = next
    setSubmittedTask(text.trim())
    setTask("")
    setAnswers({})
    setDecisions({})
    setElapsedMs(0)
    setCascadeHeaders({})
    if (!await execute({ ...next, messages: [{ role: "user", content: text.trim() }] }, emptyRunView()) && mounted.current) setTask(text)
  }

  async function resume(approvalDecisions = decisions) {
    const current = viewRef.current
    if (running || !current || !config.current || !current.runId) return
    let messages: unknown[]
    try { messages = resumeMessages(current, answers) } catch (error) { setFormError(error instanceof Error ? error.message : "This run cannot be resumed."); return }
    const approvals = current.pendingApprovals.flatMap((item) => approvalDecisions[item.toolCallId] ? [{ toolCallId: item.toolCallId, approve: approvalDecisions[item.toolCallId].approve, reason: approvalDecisions[item.toolCallId].reason.trim() || null }] : [])
    setAnswers({})
    setDecisions({})
    await execute({ ...config.current, runId: current.runId, messages, ...(approvals.length ? { approvals } : {}) }, { ...current, status: "running", items: [...current.items, ...clientAnswerItems(current, answers)], pendingToolCalls: [], pendingApprovals: [] }, current)
  }

  function decide(toolCallId: string, approve: boolean) {
    const current = viewRef.current
    if (!current) return
    const next = { ...decisions, [toolCallId]: { approve, reason: decisions[toolCallId]?.reason ?? "" } }
    setDecisions(next)
    // Approvals alone resume as soon as every call has a decision.
    if (!current.pendingToolCalls.length && current.pendingApprovals.every((item) => next[item.toolCallId])) void resume(next)
  }

  function reset() {
    if (running) return
    setView(null); viewRef.current = null; setSubmittedTask(""); setFormError(null); setElapsedMs(0); setCascadeHeaders({}); config.current = null
  }

  const status = view ? statusLabels[view.status] : null
  const pending = view?.status === "requires_action" ? view : null
  const canResume = pending && pending.pendingToolCalls.every((call) => (answers[call.id] ?? "").length > 0) && pending.pendingApprovals.every((item) => decisions[item.toolCallId])
  const timelineFiles = view ? view.files.filter((file) => !view.items.some((item) => item.kind === "tool" && item.files.includes(file))) : []
  const locked = running || pending !== null

  return <div className="flex min-h-0 flex-1 flex-col gap-4 px-4 pb-4 sm:px-6 lg:flex-row lg:px-8">
    <aside aria-label="Run settings" className="flex shrink-0 flex-col gap-5 overflow-y-auto rounded-2xl border bg-card p-4 lg:w-[320px]">
      <section className="space-y-2"><SectionTitle icon={BotIcon}>Model</SectionTitle><ModelPicker options={options} selected={selected} disabled={locked} onSelect={setModelId} /></section>

      <section className="space-y-1"><SectionTitle icon={WrenchIcon}>Tools</SectionTitle>
        {AGENT_TOOLS.map((tool) => {
          const refused = disabled[tool.name]
          return <label key={tool.name} className={cn("flex items-start gap-3 rounded-xl px-2 py-2 hover:bg-muted/40", refused && "opacity-70")}>
            <span className="min-w-0 flex-1"><span className="flex items-center gap-1.5 text-[13px]">{tool.label}{tool.hosted ? <span className="rounded border px-1 text-[10px] text-muted-foreground">hosted</span> : null}</span>
              <span className="block text-[11px] leading-4 text-muted-foreground">{refused ? <span className="text-destructive">Not enabled on this stamp</span> : tool.description}</span></span>
            <Switch size="sm" className="mt-1" checked={tools.includes(tool.name) && !refused} disabled={locked || !!refused} onCheckedChange={(checked) => setTools((value) => checked ? [...value, tool.name] : value.filter((item) => item !== tool.name))} aria-label={tool.label} />
          </label>
        })}
        <div className={cn("rounded-xl px-2 py-2", fileSearch && "bg-muted/30")}>
          <label className="flex items-start gap-3">
            <span className="min-w-0 flex-1"><span className="flex items-center gap-1.5 text-[13px]"><BookOpenTextIcon className="size-3.5 text-muted-foreground" />File search<span className="rounded border px-1 text-[10px] text-muted-foreground">hosted</span></span>
              <span className="block text-[11px] leading-4 text-muted-foreground">{disabled.file_search ? <span className="text-destructive">Not enabled on this stamp</span> : vectorStoreNotice ?? (vectorStores.length ? "Search your vector stores, with citations" : "No vector stores yet. Upload files through /v1/files.")}</span></span>
            <Switch size="sm" className="mt-1" checked={fileSearch} disabled={locked || !!disabled.file_search || !vectorStores.length} onCheckedChange={setFileSearch} aria-label="File search" />
          </label>
          {fileSearch ? <fieldset className="mt-2 space-y-1 border-t pt-2"><legend className="sr-only">Vector stores</legend>
            {vectorStores.map((store) => <label key={store.id} className="flex items-center gap-2 rounded-lg px-1 py-1 text-[12px]">
              <Checkbox checked={storeIds.includes(store.id)} disabled={locked || (!storeIds.includes(store.id) && storeIds.length >= 10)} onCheckedChange={(checked) => setStoreIds((value) => checked ? [...value, store.id] : value.filter((id) => id !== store.id))} />
              <span className="min-w-0 flex-1 truncate">{store.name}</span><span className="text-[11px] tabular-nums text-muted-foreground">{store.files} file{store.files === 1 ? "" : "s"}</span>
            </label>)}
          </fieldset> : null}
        </div>
      </section>

      <section className="space-y-2"><SectionTitle icon={WorkflowIcon} action={<Button type="button" variant="ghost" size="sm" className="h-6 gap-1 px-1.5 text-[11px]" disabled={locked || clientTools.length >= AGENT_MAX_CLIENT_TOOLS} onClick={() => setClientTools((value) => [...value, { name: "", description: "", parameters: "" }])}><PlusIcon className="size-3" />Add</Button>}>Client tools</SectionTitle>
        {clientTools.length ? clientTools.map((tool, index) => <div key={index} className="space-y-1.5 rounded-xl border p-2">
          <div className="flex gap-1.5"><Input value={tool.name} onChange={(event) => setClientTools((value) => value.map((item, position) => position === index ? { ...item, name: event.target.value } : item))} placeholder="lookup_order" aria-label="Client tool name" disabled={locked} className="h-7 font-mono text-xs md:text-xs" />
            <Button type="button" variant="ghost" size="icon-sm" disabled={locked} onClick={() => setClientTools((value) => value.filter((_, position) => position !== index))} aria-label="Remove client tool"><Trash2Icon className="size-3.5" /></Button></div>
          <Input value={tool.description} onChange={(event) => setClientTools((value) => value.map((item, position) => position === index ? { ...item, description: event.target.value } : item))} placeholder="What the tool does" aria-label="Client tool description" disabled={locked} className="h-7 text-xs md:text-xs" />
          <Textarea value={tool.parameters} onChange={(event) => setClientTools((value) => value.map((item, position) => position === index ? { ...item, parameters: event.target.value } : item))} placeholder='{"type": "object", "properties": {"order_id": {"type": "string"}}}' aria-label="Client tool parameters (JSON schema)" disabled={locked} className="min-h-14 font-mono text-[11px] md:text-[11px]" />
        </div>) : <p className="px-1 text-[11px] leading-4 text-muted-foreground">Tools without an executor pause the run so you can answer them here.</p>}
      </section>

      <section className="space-y-2"><SectionTitle icon={PlugIcon} action={<Button type="button" variant="ghost" size="sm" className="h-6 gap-1 px-1.5 text-[11px]" disabled={locked || mcpServers.length >= AGENT_MAX_MCP_SERVERS} onClick={() => setMcpServers((value) => [...value, { label: "", url: "", allowedTools: "", requireApproval: "always", authorization: "" }])}><PlusIcon className="size-3" />Add</Button>}>MCP servers</SectionTitle>
        {disabled.mcp ? <p className="px-1 text-[11px] text-destructive">{disabled.mcp}</p> : null}
        {mcpServers.length ? mcpServers.map((server, index) => {
          const update = (patch: Partial<McpDraft>) => setMcpServers((value) => value.map((item, position) => position === index ? { ...item, ...patch } : item))
          return <div key={index} className="space-y-1.5 rounded-xl border p-2">
            <div className="flex gap-1.5"><Input value={server.label} onChange={(event) => update({ label: event.target.value })} placeholder="label" aria-label="MCP server label" disabled={locked} className="h-7 font-mono text-xs md:text-xs" />
              <Button type="button" variant="ghost" size="icon-sm" disabled={locked} onClick={() => setMcpServers((value) => value.filter((_, position) => position !== index))} aria-label="Remove MCP server"><Trash2Icon className="size-3.5" /></Button></div>
            <Input value={server.url} onChange={(event) => update({ url: event.target.value })} placeholder="https://mcp.example.com/mcp" aria-label="MCP server URL" disabled={locked} className="h-7 font-mono text-xs md:text-xs" />
            <Input value={server.allowedTools} onChange={(event) => update({ allowedTools: event.target.value })} placeholder="Allowed tools (comma separated, optional)" aria-label="Allowed tools" disabled={locked} className="h-7 text-xs md:text-xs" />
            <Input type="password" value={server.authorization} onChange={(event) => update({ authorization: event.target.value })} placeholder="Authorization header (optional)" aria-label="MCP server Authorization header" autoComplete="off" disabled={locked} className="h-7 text-xs md:text-xs" />
            <label className="flex items-center justify-between gap-2 px-1 text-[12px]">Ask before each call<Switch size="sm" checked={server.requireApproval === "always"} disabled={locked} onCheckedChange={(checked) => update({ requireApproval: checked ? "always" : "never" })} /></label>
          </div>
        }) : <p className="px-1 text-[11px] leading-4 text-muted-foreground">The gateway connects to allowlisted MCP servers and can ask for your approval before each call.</p>}
      </section>

      <section className="grid grid-cols-2 gap-3">
        <label className="space-y-1.5 text-[12px]"><span className="text-muted-foreground">Max steps</span><Input type="number" min={1} max={AGENT_MAX_STEPS} value={maxSteps} disabled={locked} onChange={(event) => setMaxSteps(Number(event.target.value))} className="h-8" /></label>
        <label className="space-y-1.5 text-[12px]"><span className="text-muted-foreground">Token budget</span><Input type="number" min={1} placeholder="None" value={budget} disabled={locked} onChange={(event) => setBudget(event.target.value)} className="h-8" /></label>
        <p className="col-span-2 text-[11px] leading-4 text-muted-foreground">Max steps caps model calls in one request. The budget caps the whole run, including resumes; the gateway refuses the next call once it is spent.</p>
      </section>
    </aside>

    <div className="flex min-h-[640px] min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border bg-card lg:min-h-0">
      <header className="flex min-h-14 shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-b px-5 py-2">
        {status ? <Status tone={status.tone} pulse={view?.status === "running" || view?.status === "starting"} className="text-[13px] font-medium">{status.label}</Status> : <span className="text-[13px] font-medium">New run</span>}
        {view?.runId ? <Link href={`/agents/runs/${encodeURIComponent(view.runId)}`} className="inline-flex min-w-0 items-center gap-1 font-mono text-[12px] text-muted-foreground hover:text-foreground hover:underline"><span className="truncate">{view.runId}</span><ExternalLinkIcon className="size-3 shrink-0" /></Link> : null}
        {view ? <span className="ml-auto text-[12px] tabular-nums text-muted-foreground">{view.items.filter((item) => item.kind === "model").length} model · {view.items.filter((item) => item.kind === "tool").length} tool steps · {(view.usage.input + view.usage.output).toLocaleString("en")} tokens · {(elapsedMs / 1000).toFixed(1)} s</span> : <span className="ml-auto text-[12px] text-muted-foreground">Server-side tool loop · streamed</span>}
        {view && !running ? <Button type="button" variant="ghost" size="icon-sm" onClick={reset} aria-label="New run"><RotateCcwIcon className="size-4" /></Button> : null}
      </header>
      {Object.keys(cascadeHeaders).length ? <p className="shrink-0 border-b bg-muted/15 px-5 py-1.5 font-mono text-[11px] text-muted-foreground">{Object.entries(cascadeHeaders).map(([name, value]) => `${name.replace("X-Fabric-Cascade-", "cascade ").toLowerCase()}: ${value}`).join(" · ")}</p> : null}

      {view ? <Conversation className="min-h-0 flex-1" initial="instant" resize="smooth">
        <ConversationContent className="gap-5 px-5 py-6 lg:px-8">
          <Message from="user"><MessageContent className="whitespace-pre-wrap text-[13.5px] leading-6">{submittedTask}</MessageContent></Message>
          <ol aria-label="Run timeline" className="pt-1">
            {view.items.map((item, index) => item.kind === "model" ? <ModelStep key={item.key} item={item} last={index === view.items.length - 1 && !view.answer} /> : <ToolStep key={item.key} item={item} last={index === view.items.length - 1 && !view.answer} />)}
            {view.status === "starting" || (running && !view.items.length) ? <li className="pl-10 text-[13px] text-muted-foreground"><Shimmer duration={1.5}>Starting the run…</Shimmer></li> : null}
          </ol>

          {pending ? <section aria-label="Waiting for you" className="space-y-3 rounded-2xl border border-status-warning/30 bg-status-warning/5 p-4">
            <div className="flex items-center gap-2 text-[13px] font-medium"><ShieldAlertIcon className="size-4 text-status-warning" />The run is paused and waiting for you</div>
            {pending.pendingApprovals.map((item) => {
              const decision = decisions[item.toolCallId]
              return <div key={item.toolCallId} className="space-y-2 rounded-xl border bg-card p-3">
                <div className="flex flex-wrap items-center gap-2 text-[13px]"><PlugIcon className="size-3.5 text-muted-foreground" /><span className="font-mono font-medium">{item.serverLabel}</span><span className="text-muted-foreground">wants to call</span><span className="font-mono font-medium">{item.name}</span></div>
                <pre className="max-h-32 overflow-auto rounded-lg bg-muted/40 px-3 py-2 font-mono text-[11.5px] whitespace-pre-wrap break-all">{summariseArguments(item.arguments)}</pre>
                <Input value={decision?.reason ?? ""} onChange={(event) => setDecisions((value) => ({ ...value, [item.toolCallId]: { approve: value[item.toolCallId]?.approve ?? false, reason: event.target.value } }))} placeholder="Reason, if you deny (optional)" maxLength={500} disabled={running} className="h-8 text-xs md:text-xs" aria-label={`Reason for ${item.name}`} />
                <div className="flex gap-2"><Button type="button" size="sm" disabled={running} variant={decision?.approve === true ? "default" : "outline"} onClick={() => decide(item.toolCallId, true)}><ShieldCheckIcon />Approve</Button><Button type="button" size="sm" disabled={running} variant={decision?.approve === false ? "destructive" : "outline"} onClick={() => decide(item.toolCallId, false)}><XIcon />Deny</Button></div>
              </div>
            })}
            {pending.pendingToolCalls.map((call) => <div key={call.id} className="space-y-2 rounded-xl border bg-card p-3">
              <div className="flex flex-wrap items-center gap-2 text-[13px]"><WorkflowIcon className="size-3.5 text-muted-foreground" />Client tool <span className="font-mono font-medium">{call.name}</span></div>
              <p className="font-mono text-[11.5px] text-muted-foreground">{summariseArguments(call.arguments)}</p>
              <Textarea value={answers[call.id] ?? ""} onChange={(event) => setAnswers((value) => ({ ...value, [call.id]: event.target.value }))} placeholder='Tool result, for example {"status": "delivered"}' disabled={running} className="min-h-16 font-mono text-[12px] md:text-[12px]" aria-label={`Result for ${call.name}`} />
            </div>)}
            {pending.pendingToolCalls.length ? <Button type="button" disabled={running || !canResume} onClick={() => { void resume() }}><ArrowUpIcon />Resume run</Button> : null}
          </section> : null}

          {view.answer !== null ? <Message from="assistant" className="max-w-full gap-3">
            <div className="flex items-center gap-2 text-[11px] font-medium text-muted-foreground"><span className="flex size-6 items-center justify-center rounded-lg border"><SparklesIcon className="size-3" /></span>Final answer{view.model ? ` · ${view.model}` : ""}</div>
            <MessageContent className="w-full overflow-visible text-[14px] leading-7"><MessageResponse>{view.answer || "_The model returned an empty answer._"}</MessageResponse></MessageContent>
            {timelineFiles.length ? <div className="grid gap-2 sm:grid-cols-2">{timelineFiles.map((file) => <AgentImage key={`${file.toolCallId}-${file.name}`} file={file} />)}</div> : null}
            <CitationList citations={view.citations} />
          </Message> : null}
          {view.status === "max_steps_reached" ? <p className="rounded-xl border bg-muted/20 px-4 py-3 text-[13px] text-muted-foreground">The run stopped at its {view.stopReason === "max_tool_calls" ? "tool-call" : "step"} limit before the model answered. Raise max steps and run again, or read the trace.</p> : null}
          {view.status === "stopped" ? <p className="text-[13px] text-muted-foreground">You stopped the run. The gateway cancelled in-flight tool calls and made no further model calls.</p> : null}
          {view.error && view.status !== "stopped" ? <div role="alert" className="flex items-start gap-2 rounded-xl border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive"><CircleAlertIcon className="mt-0.5 size-4 shrink-0" />{view.error}</div> : null}
        </ConversationContent>
        <ConversationScrollButton aria-label="Scroll to the latest step" className="bottom-3 size-8" />
      </Conversation> : <div className="flex min-h-0 flex-1 flex-col overflow-auto px-5 py-8 text-center"><div className="my-auto flex flex-col items-center">
        <span className="mb-4 flex size-12 items-center justify-center rounded-2xl border bg-background shadow-sm"><WorkflowIcon className="size-5 text-foreground/70" /></span>
        <h2 className="text-[23px] font-medium tracking-[-.035em]">{options.length ? "Give the agent a task" : "Waiting for your models"}</h2>
        <p className="mt-2 max-w-md text-[13px] leading-6 text-muted-foreground">{options.length ? "The gateway calls the model, runs the tools you allow, and loops until it has an answer. Every step streams here and is traced under the run id." : "Agent runs need a ready deployment behind an approved inference gateway."}</p>
        <div className="mt-4 flex max-w-full flex-wrap justify-center gap-2">{starters.map((starter) => <Suggestion key={starter.title} suggestion={starter.task} disabled={!options.length} onClick={(text) => { setTask(text); setTools((value) => [...new Set([...value, ...starter.tools.filter((tool) => !disabled[tool])])]); document.getElementById("agent-task")?.focus() }} className="h-9 rounded-xl border-border/80 bg-background/60 px-3 text-[12px] font-normal">{starter.title}</Suggestion>)}</div>
      </div></div>}

      <form className="shrink-0 border-t bg-muted/10 p-3" onSubmit={(event) => { event.preventDefault(); void start() }}>
        {formError ? <div role="alert" className="mb-2 flex items-start justify-between gap-3 rounded-xl border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs text-destructive">{formError}<button type="button" aria-label="Dismiss error" onClick={() => setFormError(null)}><XIcon className="size-3.5" /></button></div> : null}
        <div className="flex items-end gap-2 rounded-2xl border bg-card p-2 shadow-[0_3px_14px_rgb(0_0_0/0.035)]">
          <Textarea id="agent-task" value={task} onChange={(event) => setTask(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void start() } }}
            placeholder={running ? "The agent is working…" : pending ? "Answer the paused run above, or start a new one" : "Describe a task for the agent…"} maxLength={AGENT_MAX_TASK_CHARS} disabled={running || !options.length} aria-label="Agent task"
            className="max-h-40 min-h-11 flex-1 resize-none border-0 bg-transparent px-2 py-2 text-[14px] shadow-none md:text-[14px] disabled:bg-transparent focus-visible:ring-0 dark:bg-transparent" />
          {running ? <Button type="button" onClick={() => controller.current?.abort()} className="size-9 rounded-full p-0" aria-label="Stop run"><SquareIcon className="size-3 fill-current" /></Button>
            : <Button type="submit" disabled={!selected || !task.trim()} className="size-9 rounded-full p-0" aria-label="Run agent"><ArrowUpIcon className="size-4" /></Button>}
        </div>
        <p className="mt-2 text-center text-[10px] text-muted-foreground">Enter to run · Shift + Enter for a new line · {selected ? <span className="font-mono">{selected.label}</span> : "no model"} · {tools.filter((tool) => !disabled[tool]).length + (fileSearch ? 1 : 0) + clientTools.length + mcpServers.length} tool sources · {maxSteps} steps{budget.trim() ? ` · ${Number(budget).toLocaleString("en")} token budget` : ""}</p>
      </form>
    </div>
  </div>
}
