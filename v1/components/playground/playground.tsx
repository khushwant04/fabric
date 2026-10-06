"use client"

import { useEffect, useRef, useState } from "react"
import {
  ArrowDownToLineIcon, ArrowLeftRightIcon, ArrowUpIcon, BracesIcon, CheckIcon,
  ChevronDownIcon, CopyIcon, GitCompareArrowsIcon, LightbulbIcon, RotateCcwIcon,
  Settings2Icon, ShieldCheckIcon, SparklesIcon, SquareIcon, XIcon,
} from "lucide-react"

import { Conversation, ConversationContent, ConversationScrollButton } from "@/components/ai-elements/conversation"
import { Message, MessageAction, MessageActions, MessageContent, MessageResponse } from "@/components/ai-elements/message"
import {
  ModelSelector, ModelSelectorContent, ModelSelectorEmpty, ModelSelectorGroup,
  ModelSelectorInput, ModelSelectorItem, ModelSelectorList, ModelSelectorName, ModelSelectorTrigger,
} from "@/components/ai-elements/model-selector"
import { PromptInput, PromptInputBody, PromptInputFooter, PromptInputTextarea, type PromptInputMessage } from "@/components/ai-elements/prompt-input"
import { Reasoning, ReasoningContent, ReasoningTrigger } from "@/components/ai-elements/reasoning"
import { Shimmer } from "@/components/ai-elements/shimmer"
import { Suggestion } from "@/components/ai-elements/suggestion"
import { useCopyFeedback } from "@/components/ai-elements/use-copy-feedback"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { NativeSelect } from "@/components/ui/native-select"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { MAX_PROMPT_CHARS, routingDiagnostics, type PlaygroundOption, type PlaygroundRequest, type RoutingTask } from "@/lib/fabric/playground"
import { PlaygroundStreamParser, completionTokensPerSecond, type StreamUsage } from "@/lib/fabric/playground-stream"
import { cn } from "@/lib/utils"

const starters = [
  { title: "Write better code", icon: BracesIcon, prompt: "Write a Python LRU cache with a fixed capacity. Explain its time complexity and include a short usage example." },
  { title: "Think it through", icon: LightbulbIcon, prompt: "A service gets slower when we add more workers. What could explain this? Walk through how you would investigate it." },
  { title: "Make it click", icon: SparklesIcon, prompt: "Explain how a GPU runs thousands of operations at once. Use a concrete analogy, then explain where the analogy breaks down." },
]

type Result = {
  status: "idle" | "running" | "complete" | "stopped" | "error"
  label: string
  content: string
  reasoning: string
  elapsedMs: number
  firstContentMs: number | null
  usage: StreamUsage | null
  diagnostics: Record<string, string>
  finishReason: string | null
  error: string | null
  request: PlaygroundRequest | null
  runId: number
}

const emptyResult = (): Result => ({ status: "idle", label: "", content: "", reasoning: "", elapsedMs: 0, firstContentMs: null, usage: null, diagnostics: {}, finishReason: null, error: null, request: null, runId: 0 })
const seconds = (ms: number | null) => ms === null ? "—" : `${(ms / 1000).toFixed(2)}s`
const diagnosticLabels: Record<string, string> = {
  "X-Fabric-Selected-Model": "Selected model", "X-Fabric-Routing-Task": "Task",
  "X-Fabric-Routing-Reason": "Selection reason", "X-Fabric-Routing-Policy": "Policy",
}

function decodeAlias(value: string) {
  try { return decodeURIComponent(value).replace(/[\u0000-\u001f\u007f]/g, "") } catch { return value }
}

function ModelPicker({ option, options, index, disabled, onSelect }: {
  option?: PlaygroundOption
  options: PlaygroundOption[]
  index: number
  disabled: boolean
  onSelect: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
  return <ModelSelector open={open} onOpenChange={setOpen}>
    <ModelSelectorTrigger render={<Button variant="ghost" disabled={disabled || !options.length} className="h-auto min-w-0 flex-1 justify-start gap-3 rounded-xl px-2 py-2 text-left font-normal" />} aria-label={`Choose model ${index === 0 ? "A" : "B"}`}>
      <span className={cn("flex size-9 shrink-0 items-center justify-center rounded-xl border text-xs font-semibold", index === 0 ? "border-orange-500/15 bg-orange-500/5 text-orange-600 dark:text-orange-300" : "border-violet-500/15 bg-violet-500/5 text-violet-600 dark:text-violet-300")}>{index === 0 ? "A" : "B"}</span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5"><span className="truncate text-[13px] font-medium">{option?.modelAlias ?? "No model available"}</span><span className="truncate text-[11px] text-muted-foreground">{option?.model === "auto" ? "Automatic gateway selection" : option ? option.label.split(" · ").slice(1).join(" · ") || "Ready deployment" : "Waiting for a ready deployment"}</span></span>
      <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground" />
    </ModelSelectorTrigger>
    <ModelSelectorContent title={`Choose model ${index === 0 ? "A" : "B"}`} className="sm:max-w-md">
      <ModelSelectorInput placeholder="Search your deployments…" />
      <ModelSelectorList><ModelSelectorEmpty>No matching deployment.</ModelSelectorEmpty><ModelSelectorGroup heading="Available for inference">
        {options.map((item) => <ModelSelectorItem key={item.id} value={`${item.id} ${item.label}`} onSelect={() => { onSelect(item.id); setOpen(false) }} className="gap-3 py-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border bg-muted/40"><GitCompareArrowsIcon className="size-4 text-muted-foreground" /></span>
          <ModelSelectorName><span className="block text-sm font-medium">{item.modelAlias}</span><span className="block truncate text-xs text-muted-foreground">{item.label}</span></ModelSelectorName>
          {item.id === option?.id ? <CheckIcon className="size-4" /> : <span className="size-1.5 rounded-full bg-emerald-500" />}
        </ModelSelectorItem>)}
      </ModelSelectorGroup></ModelSelectorList>
    </ModelSelectorContent>
  </ModelSelector>
}

function RunStatus({ result, available }: { result: Result; available: boolean }) {
  const label = result.status === "idle" ? available ? "Ready" : "Unavailable" : result.status === "running" ? "Generating" : result.status === "complete" ? "Complete" : result.status === "stopped" ? "Stopped" : "Failed"
  return <span role="status" className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-foreground"><span className={cn("size-1.5 rounded-full", result.status === "error" ? "bg-destructive" : result.status === "running" ? "animate-pulse bg-emerald-500" : available ? "bg-emerald-500" : "bg-muted-foreground/50")} />{label}</span>
}

function ResponsePane({ result, index, active, onRetry, busy }: { result: Result; index: number; active: boolean; onRetry: () => void; busy: boolean }) {
  const { copied, copy: copyText } = useCopyFeedback()
  const rate = completionTokensPerSecond(result.usage, result.elapsedMs, result.status === "complete")
  const streamingReasoning = result.status === "running" && !result.content
  async function copy() { await copyText(result.content) }
  return <section id={`playground-response-${index}`} aria-label={`Response ${index === 0 ? "A" : "B"}`} className={cn("min-h-0 min-w-0 flex-col md:flex", active ? "flex" : "hidden")}>
    <Conversation className="min-h-0 flex-1" initial="instant" resize="smooth">
      <ConversationContent className="gap-5 px-5 py-6 lg:px-7">
        {result.status === "idle" ? <div className="flex min-h-48 items-center justify-center text-sm text-muted-foreground">Send a prompt to see this model&apos;s response.</div> : <Message from="assistant" className="max-w-full gap-4">
          <div className="flex items-center gap-2 text-[11px] font-medium text-muted-foreground"><span className="flex size-6 items-center justify-center rounded-lg border"><SparklesIcon className="size-3" /></span>{result.label}</div>
          <MessageContent className="w-full overflow-visible text-[14px] leading-7">
            {result.reasoning ? <Reasoning key={result.runId} isStreaming={streamingReasoning} className="mb-1"><ReasoningTrigger className="text-xs" getThinkingMessage={(thinking) => thinking ? <Shimmer duration={1}>Thinking…</Shimmer> : "Model reasoning"} /><ReasoningContent className="mt-3 border-l pl-3 text-sm leading-6">{result.reasoning}</ReasoningContent></Reasoning> : null}
            {result.content ? <MessageResponse isAnimating={result.status === "running"}>{result.content}</MessageResponse> : result.status === "running" ? <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground"><span className="size-2 animate-pulse rounded-full bg-foreground/30" /><Shimmer duration={1.5}>{result.reasoning ? "Preparing an answer…" : "Waiting for the first token…"}</Shimmer></div> : null}
            {result.error ? <div role="alert" className="rounded-xl border border-destructive/20 bg-destructive/5 px-4 py-3 text-sm text-destructive">{result.error}</div> : null}
            {result.status === "stopped" && !result.content && !result.reasoning ? <p className="text-sm text-muted-foreground">Generation stopped before an answer arrived.</p> : null}
            {result.finishReason === "length" ? <p className="text-xs text-muted-foreground">Output limit reached. Increase the limit for a longer answer.</p> : null}
          </MessageContent>
          {result.status !== "running" ? <MessageActions className="gap-0.5">
            <MessageAction tooltip={copied ? "Copied" : "Copy answer"} onClick={copy} disabled={!result.content} className="rounded-lg">{copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}</MessageAction>
            <MessageAction tooltip="Regenerate this response" onClick={onRetry} disabled={busy || !result.request} className="rounded-lg"><RotateCcwIcon className="size-3.5" /></MessageAction>
          </MessageActions> : null}
          {Object.keys(result.diagnostics).length ? <details className="rounded-xl border bg-muted/20 px-3 py-2 text-[11px]"><summary className="cursor-pointer text-muted-foreground">Routing details</summary><dl className="mt-3 space-y-2">{Object.entries(result.diagnostics).map(([name, value]) => <div className="flex justify-between gap-4" key={name}><dt className="text-muted-foreground">{diagnosticLabels[name]}</dt><dd className="break-words text-right">{name === "X-Fabric-Selected-Model" ? decodeAlias(value) : value}</dd></div>)}</dl></details> : null}
        </Message>}
      </ConversationContent>
      <ConversationScrollButton aria-label={`Scroll response ${index === 0 ? "A" : "B"} to bottom`} className="bottom-3 size-8" />
    </Conversation>
    <dl className="grid shrink-0 grid-cols-4 gap-2 border-t bg-muted/15 px-5 py-3 lg:px-7">
      {[{ label: "First answer", value: seconds(result.firstContentMs) }, { label: "Elapsed", value: result.status === "idle" ? "—" : seconds(result.elapsedMs) }, { label: "Output tokens", value: result.usage?.output.toLocaleString() ?? "—" }, { label: "Tokens / sec", value: rate === null ? "—" : rate.toFixed(1) }].map((metric) => <div key={metric.label}><dt className="text-[10px] text-muted-foreground">{metric.label}</dt><dd className="mt-1 font-mono text-xs tabular-nums">{metric.value}</dd></div>)}
    </dl>
  </section>
}

async function streamResponse(index: number, label: string, request: PlaygroundRequest, runId: number, controller: AbortController, update: (index: number, result: Result) => void) {
  const start = performance.now()
  const result: Result = { ...emptyResult(), label, request, runId, status: "running" }
  update(index, result)
  const timer = setInterval(() => { result.elapsedMs = performance.now() - start; update(index, result) }, 100)
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    const response = await fetch("/api/playground", { method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal, body: JSON.stringify(request) })
    if (!response.ok) {
      let message = `Inference request failed (${response.status}).`
      try { const payload = await response.json(); if (typeof payload.error === "string") message = payload.error } catch { /* Keep a safe failure message. */ }
      throw new Error(message)
    }
    if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) throw new Error("Inference did not return a stream.")
    result.diagnostics = routingDiagnostics(response.headers)
    const parser = new PlaygroundStreamParser()
    const decoder = new TextDecoder()
    reader = response.body.getReader()
    let complete = false
    while (!complete) {
      const chunk = await reader.read()
      if (chunk.done) break
      for (const frame of parser.feed(decoder.decode(chunk.value, { stream: true }))) {
        if (frame.content && result.firstContentMs === null) result.firstContentMs = performance.now() - start
        result.content += frame.content
        result.reasoning += frame.reasoning
        if (frame.usage) result.usage = frame.usage
        if (frame.finishReason) result.finishReason = frame.finishReason
        complete ||= frame.done
      }
      if (result.content.length + result.reasoning.length > 1_000_000) throw new Error("The response exceeded the playground display limit.")
      result.elapsedMs = performance.now() - start
      update(index, result)
    }
    parser.finish()
    result.status = "complete"
  } catch (error) {
    result.status = controller.signal.aborted ? "stopped" : "error"
    result.error = controller.signal.aborted ? null : error instanceof Error ? error.message : "Inference interrupted."
  } finally {
    clearInterval(timer)
    result.elapsedMs = performance.now() - start
    update(index, result)
    try { await reader?.cancel() } catch { /* The connection may already be closed. */ }
  }
}

export function Playground({ options }: { options: PlaygroundOption[] }) {
  const exact = options.filter((item) => item.model === "exact")
  const initial = exact.length ? exact : options
  const [selected, setSelected] = useState<[string, string]>([initial[0]?.id ?? "", initial[1]?.id ?? initial[0]?.id ?? ""])
  const [prompt, setPrompt] = useState("")
  const [maxTokens, setMaxTokens] = useState(512)
  const [temperature, setTemperature] = useState(0.7)
  const [task, setTask] = useState<RoutingTask | "">("")
  const [results, setResults] = useState<[Result, Result]>([emptyResult(), emptyResult()])
  const [submittedPrompt, setSubmittedPrompt] = useState("")
  const [formError, setFormError] = useState<string | null>(null)
  const { copied, copy: copyText } = useCopyFeedback()
  const [activePane, setActivePane] = useState(0)
  const runCounter = useRef(0)
  const currentRuns = useRef<[number, number]>([0, 0])
  const controllers = useRef<AbortController[]>([])
  const mounted = useRef(true)
  const submitting = useRef(false)
  const running = results.some((result) => result.status === "running")
  const hasRun = results.some((result) => result.status !== "idle")
  const currentSelected = selected.map((id, index) => options.some((option) => option.id === id) || results[index].status !== "idle" ? id : initial[index]?.id ?? initial[0]?.id ?? "")
  const chosen = currentSelected.map((id) => options.find((option) => option.id === id))
  const modelsAvailable = chosen.every((option) => !!option)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; controllers.current.forEach((controller) => controller.abort()) }
  }, [])

  function update(index: number, result: Result) {
    if (!mounted.current || result.runId !== currentRuns.current[index]) return
    setResults((current) => { const next: [Result, Result] = [...current]; next[index] = { ...result }; return next })
  }

  function chooseModel(index: number, id: string) {
    if (running || submitting.current || id === currentSelected[index]) return
    const next: [string, string] = [currentSelected[0], currentSelected[1]]
    next[index] = id
    currentRuns.current[index] = ++runCounter.current
    setSelected(next)
    setResults((current) => { const updated: [Result, Result] = [...current]; updated[index] = emptyResult(); return updated })
  }

  function swapModels() {
    if (running || submitting.current || currentSelected[0] === currentSelected[1]) return
    currentRuns.current = [currentRuns.current[1], currentRuns.current[0]]
    setSelected([currentSelected[1], currentSelected[0]])
    setResults((current) => [current[1], current[0]])
    setActivePane((current) => current === 0 ? 1 : 0)
  }


  async function submit(message: PromptInputMessage) {
    if (running || submitting.current || !message.text.trim() || chosen.some((item) => !item)) return
    if (message.files.length) { setFormError("This comparison accepts text prompts. Paste the text you want the models to read."); throw new Error("Text prompt required") }
    if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 2048 || !Number.isFinite(temperature) || temperature < 0 || temperature > 2) { setFormError("Choose an output limit from 1 to 2048 and a temperature from 0 to 2."); throw new Error("Invalid generation settings") }
    submitting.current = true
    setFormError(null)
    setSubmittedPrompt(message.text.trim())
    setActivePane(0)
    const runId = ++runCounter.current
    currentRuns.current = [runId, runId]
    controllers.current = [new AbortController(), new AbortController()]
    const requests = chosen.map((option, index) => option ? streamResponse(index, option.modelAlias, { deploymentId: option.deploymentId, model: option.model, prompt: message.text.trim(), maxTokens, temperature, ...(task ? { task } : {}) }, runId, controllers.current[index], update) : Promise.resolve())
    setPrompt("")
    try { await Promise.allSettled(requests) } finally { submitting.current = false }
  }

  async function retry(index: number) {
    const previous = results[index]
    if (running || submitting.current || !previous.request) return
    submitting.current = true
    const runId = ++runCounter.current
    currentRuns.current[index] = runId
    const controller = new AbortController()
    controllers.current[index] = controller
    try { await streamResponse(index, previous.label, previous.request, runId, controller, update) } finally { submitting.current = false }
  }

  function reset() {
    if (submitting.current) return
    const runId = ++runCounter.current
    currentRuns.current = [runId, runId]
    controllers.current.forEach((controller) => controller.abort())
    setResults([emptyResult(), emptyResult()])
    setSubmittedPrompt("")
    setPrompt("")
    setFormError(null)
  }

  async function copyRequest() {
    const option = chosen[0]
    if (!option) return
    const payload = { model: option.modelAlias, messages: [{ role: "user", content: prompt.trim() || submittedPrompt }], max_tokens: maxTokens, temperature, stream: true, stream_options: { include_usage: true }, routing: { ...(task ? { task } : {}), explain: true } }
    const shellBody = JSON.stringify(payload).replace(/'/g, "'\\''")
    await copyText(['curl --no-buffer "$FABRIC_INFERENCE_URL/v1/chat/completions"', '  -H "Authorization: Bearer $FABRIC_INFERENCE_TOKEN"', "  -H 'Content-Type: application/json'", `  --data '${shellBody}'`].join(" \\\n"))
  }

  function download() {
    const text = [`# Model comparison\n\n${submittedPrompt}`, ...results.map((result, index) => `## ${index === 0 ? "A" : "B"} · ${result.label || chosen[index]?.modelAlias || "Unavailable"}\n\n${result.content || (result.status === "idle" ? "No response generated." : result.error || "No answer text returned.")}${result.reasoning ? `\n\n<details><summary>Reasoning</summary>\n\n${result.reasoning}\n\n</details>` : ""}`)].join("\n\n")
    const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }))
    const link = document.createElement("a"); link.href = url; link.download = "fabric-comparison.md"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return <div className="flex min-h-0 flex-1 flex-col gap-4 px-4 pb-4 sm:px-6 lg:px-8">
    <div className="mx-auto flex w-full max-w-[1360px] min-w-0 items-center justify-between gap-3">
      <div className="flex items-center gap-2 text-xs text-muted-foreground"><GitCompareArrowsIcon className="size-3.5" /><span>Side by side</span><span className="mx-1 h-3 border-l" /><span>{exact.length} ready deployment{exact.length === 1 ? "" : "s"}</span></div>
      <div className="flex items-center gap-1">
        <Tooltip><TooltipTrigger render={<Button variant="ghost" size="icon-sm" className="rounded-lg" onClick={download} disabled={!hasRun || running} aria-label="Download comparison" />}><ArrowDownToLineIcon className="size-4" /></TooltipTrigger><TooltipContent>Download comparison</TooltipContent></Tooltip>
        <Tooltip><TooltipTrigger render={<Button variant="ghost" size="icon-sm" className="rounded-lg" onClick={reset} disabled={!hasRun || running} aria-label="New comparison" />}><RotateCcwIcon className="size-4" /></TooltipTrigger><TooltipContent>New comparison</TooltipContent></Tooltip>
      </div>
    </div>
    <div className="mx-auto flex min-h-0 w-full max-w-[1360px] flex-1 flex-col overflow-hidden rounded-[22px] border bg-card shadow-[0_1px_3px_rgb(0_0_0/0.02)]">
      <div className="relative grid shrink-0 grid-cols-2 border-b">
        {[0, 1].map((index) => <div key={index} className={cn("flex min-w-0 items-center gap-2 px-2 py-2 sm:px-4", index === 0 && "border-r")}>
          <ModelPicker option={chosen[index]} options={options} index={index} disabled={running} onSelect={(id) => chooseModel(index, id)} />
          <span className="hidden sm:block"><RunStatus result={results[index]} available={!!chosen[index]} /></span>
        </div>)}
        <Tooltip><TooltipTrigger render={<Button variant="outline" size="icon-sm" className="absolute left-1/2 top-1/2 z-10 size-7 -translate-x-1/2 -translate-y-1/2 rounded-full bg-card" disabled={running || !chosen[0] || !chosen[1] || currentSelected[0] === currentSelected[1]} onClick={swapModels} aria-label="Swap models" />}><ArrowLeftRightIcon className="size-3" /></TooltipTrigger><TooltipContent>Swap models and answers</TooltipContent></Tooltip>
      </div>
      {chosen[0]?.model === "exact" && chosen[1]?.model === "exact" && chosen[0].deploymentId === chosen[1].deploymentId ? <p className="shrink-0 border-b bg-muted/15 px-5 py-2 text-[11px] text-muted-foreground">Both panes use the same deployment and share its capacity.</p> : null}
      {hasRun && !modelsAvailable ? <p role="status" className="shrink-0 border-b bg-muted/15 px-5 py-2 text-[11px] text-muted-foreground">A selected deployment is no longer available. Your previous answers remain below; choose an available model before sending again.</p> : null}
      {hasRun ? <>
        <div className="flex shrink-0 items-start gap-3 border-b bg-muted/15 px-5 py-3 text-[12px] sm:px-7"><span className="shrink-0 pt-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">You</span><p className="max-h-20 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-words leading-5">{submittedPrompt}</p></div>
        <div className="grid shrink-0 grid-cols-2 border-b md:hidden" aria-label="Choose response to view">{results.map((result, index) => <button key={index} type="button" aria-pressed={activePane === index} aria-controls={`playground-response-${index}`} onClick={() => setActivePane(index)} className={cn("flex items-center justify-between gap-2 px-4 py-2.5 text-xs focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-[-2px]", activePane === index ? "bg-muted/50 font-medium" : "text-muted-foreground")}><span>Response {index === 0 ? "A" : "B"}</span><RunStatus result={result} available={!!chosen[index]} /></button>)}</div>
        <div className="grid min-h-0 flex-1 overflow-hidden md:grid-cols-2 md:divide-x">{results.map((result, index) => <ResponsePane key={`${index}-${result.runId}-${result.request?.deploymentId ?? "idle"}`} result={result} index={index} active={activePane === index} busy={running} onRetry={() => retry(index)} />)}</div>
      </> : <div className="flex min-h-0 flex-1 flex-col overflow-auto px-5 py-5 text-center md:py-8"><div className="my-auto flex shrink-0 flex-col items-center">
        <div className="relative mb-4 flex h-14 w-20 items-center justify-center"><span className="absolute left-1 flex size-12 -rotate-12 items-center justify-center rounded-2xl border bg-background shadow-sm"><SparklesIcon className="size-5 text-foreground/60" /></span><span className="absolute right-1 flex size-12 rotate-12 items-center justify-center rounded-2xl border bg-card shadow-sm"><GitCompareArrowsIcon className="size-5 text-foreground/70" /></span></div>
        <h2 className="text-[23px] font-medium tracking-[-.035em] sm:text-[27px]">{options.length ? "One prompt. Two perspectives." : "Waiting for your models"}</h2>
        <p className="mt-2 max-w-md text-[13px] leading-6 text-muted-foreground">{options.length ? <>Choose two deployments and see how they respond.<br className="hidden sm:block" /> Compare the answers, reasoning, and time to first text.</> : "Your deployments will appear here when they are ready and their inference gateway is available."}</p>
        <div className="mt-4 flex max-w-full flex-wrap justify-center gap-2">{starters.map((starter) => <Suggestion key={starter.title} suggestion={starter.prompt} onClick={(text) => { setPrompt(text); document.getElementById("playground-prompt")?.focus() }} disabled={!options.length} className="h-9 gap-2 rounded-xl border-border/80 bg-background/60 px-3 text-[12px] font-normal"><starter.icon className="size-3.5 text-muted-foreground" />{starter.title}</Suggestion>)}</div>
      </div></div>}
    </div>
    <div className="mx-auto w-full max-w-[860px] shrink-0">
      {formError ? <div role="alert" className="mb-3 flex items-start justify-between gap-3 rounded-xl border border-destructive/20 bg-destructive/5 px-4 py-3 text-xs text-destructive">{formError}<button type="button" aria-label="Dismiss prompt error" onClick={() => setFormError(null)}><XIcon className="size-3.5" /></button></div> : null}
      <PromptInput onSubmit={submit} maxFiles={0} onError={() => setFormError("Paste a text prompt to compare the models.")} inputGroupClassName="h-auto rounded-[24px] border-border bg-card shadow-[0_3px_14px_rgb(0_0_0/0.035)] has-[[data-slot=input-group-control]:focus-visible]:border-border has-[[data-slot=input-group-control]:focus-visible]:ring-0" aria-label="Comparison prompt">
        <PromptInputBody><PromptInputTextarea id="playground-prompt" value={prompt} onChange={(event) => setPrompt(event.currentTarget.value)} placeholder={running ? "Your models are responding…" : "Ask both models anything…"} aria-label="Prompt for both models" autoComplete="off" disabled={running || !options.length} maxLength={MAX_PROMPT_CHARS} className="min-h-14 px-5 pb-1 pt-4 text-[14px] leading-6 md:text-[14px]" /></PromptInputBody>
        <PromptInputFooter className="px-3 pb-3 pt-1">
          <div className="flex min-w-0 items-center gap-1">
            <Popover><PopoverTrigger render={<Button type="button" variant="ghost" size="icon-sm" disabled={running} className="rounded-full text-muted-foreground" aria-label="Generation settings" />}><Settings2Icon className="size-4" /></PopoverTrigger>
              <PopoverContent align="start" side="top" className="w-72 gap-5 p-5"><div><h3 className="text-sm font-medium">Generation settings</h3><p className="mt-1 text-xs leading-5 text-muted-foreground">The same settings apply to both models.</p></div>
                <label className="space-y-2 text-xs" htmlFor="output-limit"><span className="flex justify-between">Maximum output tokens<span className="text-muted-foreground">1–2048</span></span><Input id="output-limit" type="number" min={1} max={2048} value={maxTokens} onChange={(event) => setMaxTokens(Number(event.target.value))} /></label>
                <label className="space-y-2 text-xs" htmlFor="temperature"><span className="flex justify-between">Temperature<span className="text-muted-foreground">0–2</span></span><Input id="temperature" type="number" min={0} max={2} step={0.1} value={temperature} onChange={(event) => setTemperature(Number(event.target.value))} /></label>
                <label className="space-y-2 text-xs" htmlFor="routing-task"><span>Automatic routing task</span><NativeSelect id="routing-task" className="w-full" value={task} onChange={(event) => setTask(event.target.value as RoutingTask | "")}><option value="">Infer from prompt</option><option value="general">General</option><option value="code">Code</option><option value="reasoning">Reasoning</option></NativeSelect></label>
              </PopoverContent>
            </Popover>
            <Tooltip><TooltipTrigger render={<Button type="button" variant="ghost" size="icon-sm" disabled={!options.length} onClick={copyRequest} className="rounded-full text-muted-foreground" aria-label="Copy cURL request" />}>
              {copied ? <CheckIcon className="size-4" /> : <BracesIcon className="size-4" />}
            </TooltipTrigger><TooltipContent>{copied ? "Copied" : "Copy cURL request for model A"}</TooltipContent></Tooltip>
            <span className="ml-1 hidden truncate text-[11px] text-muted-foreground sm:block">{maxTokens} tokens<span className="mx-1.5 text-border">/</span>{temperature} temperature</span>
          </div>
          <div className="flex items-center gap-3"><span className="hidden text-[11px] text-muted-foreground sm:block">{running ? "Streaming independently" : "Send to A + B"}</span>{running ? <><button type="submit" disabled hidden aria-hidden="true" tabIndex={-1} /><Button type="button" onClick={() => controllers.current.forEach((controller) => controller.abort())} className="size-9 rounded-full p-0" aria-label="Stop both responses"><SquareIcon className="size-3 fill-current" /></Button></> : <Button type="submit" disabled={!modelsAvailable || !prompt.trim()} className="size-9 rounded-full p-0" aria-label="Compare responses"><ArrowUpIcon className="size-4" /></Button>}</div>
        </PromptInputFooter>
      </PromptInput>
      <div className="mt-2.5 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground"><span className="flex items-center gap-1.5"><ShieldCheckIcon className="size-3" />Prompts and responses stay in this page.</span><span className="hidden sm:inline">Enter to send · Shift + Enter for a new line</span>
        <Popover><PopoverTrigger className="underline decoration-muted-foreground/40 underline-offset-2">About these measurements</PopoverTrigger><PopoverContent side="top" className="w-80 p-4 text-xs leading-6">First answer includes browser network and authorization time. Tokens/sec uses host-reported output tokens divided by total request time. Both requests share deployment capacity; these measurements help you explore responses and are not a throughput benchmark.</PopoverContent></Popover>
      </div>
    </div>
  </div>
}
