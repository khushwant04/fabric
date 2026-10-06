"use client"

import { useEffect, useRef, useState, type FormEvent } from "react"
import { ArrowUpIcon, CheckIcon, CopyIcon, FlaskConicalIcon, SquareIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { NativeSelect } from "@/components/ui/native-select"
import { Textarea } from "@/components/ui/textarea"
import { MAX_PROMPT_CHARS, routingDiagnostics, type PlaygroundOption, type RoutingTask } from "@/lib/fabric/playground"
import { PlaygroundStreamParser, completionTokensPerSecond, type StreamUsage } from "@/lib/fabric/playground-stream"

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
}

const emptyResult = (): Result => ({ status: "idle", label: "", content: "", reasoning: "", elapsedMs: 0, firstContentMs: null, usage: null, diagnostics: {}, finishReason: null, error: null })
const seconds = (ms: number | null) => ms === null ? "—" : `${(ms / 1000).toFixed(2)}s`
const diagnosticLabels: Record<string, string> = {
  "X-Fabric-Selected-Model": "Selected model", "X-Fabric-Routing-Task": "Task",
  "X-Fabric-Routing-Reason": "Selection reason", "X-Fabric-Routing-Policy": "Policy",
}

function decodeAlias(value: string) {
  try { return decodeURIComponent(value).replace(/[\u0000-\u001f\u007f]/g, "") } catch { return value }
}

function ComparisonResult({ result, index, available }: { result: Result; index: number; available: boolean }) {
  const rate = completionTokensPerSecond(result.usage, result.elapsedMs, result.status === "complete")
  return (
    <Card className="min-w-0 gap-0 overflow-hidden py-0">
      <CardHeader className="gap-3 border-b py-5">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="min-w-0 truncate text-sm">{result.label || `Response ${index + 1}`}</CardTitle>
          <span role="status" className="text-xs capitalize text-muted-foreground">{result.status === "idle" ? available ? "Ready" : "Unavailable" : result.status}</span>
        </div>
        <dl className="grid grid-cols-2 gap-4 text-xs sm:grid-cols-4">
          {[
            { label: "First answer", value: seconds(result.firstContentMs) },
            { label: "Elapsed", value: result.status === "idle" ? "—" : seconds(result.elapsedMs) },
            { label: "Output tokens", value: result.usage?.output.toLocaleString() ?? "—" },
            { label: "Tokens / sec", value: rate === null ? "—" : rate.toFixed(1) },
          ].map((metric) => <div key={metric.label}><dt className="text-muted-foreground">{metric.label}</dt><dd className="mt-1.5 text-sm font-medium tabular-nums">{metric.value}</dd></div>)}
        </dl>
      </CardHeader>
      <CardContent className="space-y-5 py-5">
        {Object.keys(result.diagnostics).length ? <dl className="grid gap-2 rounded-md border bg-muted/30 p-3 text-xs">{Object.entries(result.diagnostics).map(([name, value]) => <div className="flex flex-wrap justify-between gap-2" key={name}><dt className="text-muted-foreground">{diagnosticLabels[name]}</dt><dd className="break-all font-medium">{name === "X-Fabric-Selected-Model" ? decodeAlias(value) : value}</dd></div>)}</dl> : null}
        {result.reasoning ? <details className="rounded-md border p-3"><summary className="cursor-pointer text-xs font-medium">Model reasoning</summary><div className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-words text-sm leading-6 text-muted-foreground">{result.reasoning}</div></details> : null}
        {result.content ? <div className="min-h-48 whitespace-pre-wrap break-words text-sm leading-7">{result.content}</div> : <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground"><FlaskConicalIcon className="size-4" />{result.status === "running" ? result.reasoning ? "Waiting for the answer…" : "Waiting for the model…" : "The model response will appear here."}</div>}
        {result.error ? <p role="alert" className="text-sm text-destructive">{result.error}</p> : null}
        {result.status === "complete" && !result.usage ? <p className="text-xs text-muted-foreground">The host did not report terminal token usage. Token counts and throughput are unavailable.</p> : null}
        {result.usage ? <p className="text-xs text-muted-foreground">Host-reported usage: {result.usage.input.toLocaleString()} input · {result.usage.output.toLocaleString()} output tokens.</p> : null}
        {result.finishReason === "length" ? <p className="text-xs text-muted-foreground">The model reached the output limit. Increase it to continue a longer response.</p> : null}
      </CardContent>
    </Card>
  )
}

export function Playground({ options }: { options: PlaygroundOption[] }) {
  const [selected, setSelected] = useState<[string, string]>([options[0]?.id ?? "", options[1]?.id ?? options[0]?.id ?? ""])
  const [prompt, setPrompt] = useState("")
  const [maxTokens, setMaxTokens] = useState(512)
  const [temperature, setTemperature] = useState(0.7)
  const [task, setTask] = useState<RoutingTask | "">("")
  const [results, setResults] = useState<[Result, Result]>([emptyResult(), emptyResult()])
  const [copied, setCopied] = useState(false)
  const controllers = useRef<AbortController[]>([])
  const mounted = useRef(true)
  const running = results.some((result) => result.status === "running")
  // Availability can change during a live refresh. Preserve valid selections,
  // and choose an available replacement without resetting prompts or responses.
  const currentSelected = selected.map((id, index) => options.some((option) => option.id === id)
    ? id : options[index]?.id ?? options[0]?.id ?? "")

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; controllers.current.forEach((controller) => controller.abort()) }
  }, [])

  function update(index: number, result: Result) {
    if (!mounted.current) return
    setResults((current) => {
      const next: [Result, Result] = [...current]
      next[index] = { ...result }
      return next
    })
  }

  async function generate(index: number, option: PlaygroundOption, controller: AbortController) {
    const start = performance.now()
    const result: Result = { ...emptyResult(), label: option.label, status: "running" }
    update(index, result)
    const timer = setInterval(() => { result.elapsedMs = performance.now() - start; update(index, result) }, 100)
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
    try {
      const response = await fetch("/api/playground", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ deploymentId: option.deploymentId, model: option.model, prompt, maxTokens, temperature, ...(task ? { task } : {}) }),
      })
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

  function submit(event: FormEvent) {
    event.preventDefault()
    if (running || !prompt.trim()) return
    const choices = currentSelected.map((id) => options.find((option) => option.id === id))
    if (choices.some((choice) => !choice)) return
    controllers.current = [new AbortController(), new AbortController()]
    choices.forEach((choice, index) => { if (choice) void generate(index, choice, controllers.current[index]) })
  }

  async function copyRequest() {
    const option = options.find((item) => item.id === currentSelected[0])
    if (!option) return
    // SDK credentials remain placeholders; console tokens are never rendered.
    const payload = { model: option.modelAlias, messages: [{ role: "user", content: prompt }], max_tokens: maxTokens, temperature, stream: true, stream_options: { include_usage: true }, routing: { ...(task ? { task } : {}), explain: true } }
    const shellBody = JSON.stringify(payload).replace(/'/g, "'\\''")
    try {
      await navigator.clipboard.writeText([
        'curl --no-buffer "$FABRIC_INFERENCE_URL/v1/chat/completions"',
        '  -H "Authorization: Bearer $FABRIC_INFERENCE_TOKEN"',
        "  -H 'Content-Type: application/json'",
        `  --data '${shellBody}'`,
      ].join(" \\\n"))
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { setCopied(false) }
  }

  return (
    <div className="space-y-6">
      <form onSubmit={submit}>
        <Card>
          <CardHeader><CardTitle>Compare models</CardTitle><CardDescription>Responses stream independently. Automatic selection chooses compatible models at the selected gateway.</CardDescription></CardHeader>
          <CardContent className="space-y-5">
            <div className="grid gap-5 md:grid-cols-2">{[0, 1].map((index) => <Field key={index}><FieldLabel htmlFor={`model-${index}`}>Model {index + 1}</FieldLabel><NativeSelect id={`model-${index}`} className="w-full" value={currentSelected[index]} disabled={running || !options.length} onChange={(event) => setSelected((current) => { const next: [string, string] = [...current]; next[index] = event.target.value; return next })}>{!options.length ? <option value="">No available deployment</option> : options.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</NativeSelect></Field>)}</div>
            <Field><FieldLabel htmlFor="prompt">Prompt</FieldLabel><Textarea id="prompt" name="prompt" autoComplete="off" className="min-h-32" placeholder="Ask a question, compare an argument, or debug a piece of code…" value={prompt} onChange={(event) => setPrompt(event.target.value)} disabled={running} maxLength={MAX_PROMPT_CHARS} required /><p className="text-xs text-muted-foreground">Prompts stay in this page and are sent only for generation. Fabric does not save playground history.</p></Field>
            <div className="grid gap-5 sm:grid-cols-3"><Field><FieldLabel htmlFor="output-limit">Maximum output tokens</FieldLabel><Input id="output-limit" type="number" min={1} max={2048} required value={maxTokens} onChange={(event) => setMaxTokens(Number(event.target.value))} disabled={running} /></Field><Field><FieldLabel htmlFor="temperature">Temperature</FieldLabel><Input id="temperature" type="number" min={0} max={2} step={0.1} required value={temperature} onChange={(event) => setTemperature(Number(event.target.value))} disabled={running} /></Field><Field><FieldLabel htmlFor="routing-task">Auto routing task</FieldLabel><NativeSelect id="routing-task" className="w-full" value={task} onChange={(event) => setTask(event.target.value as RoutingTask | "")} disabled={running}><option value="">Infer from prompt</option><option value="general">General</option><option value="code">Code</option><option value="reasoning">Reasoning</option></NativeSelect></Field></div>
            <div className="flex flex-wrap items-center gap-3"><Button type="submit" disabled={running || !options.length || !prompt.trim()}><ArrowUpIcon />Compare responses</Button>{running ? <Button type="button" variant="outline" onClick={() => controllers.current.forEach((controller) => controller.abort())}><SquareIcon />Stop both</Button> : null}<Button type="button" variant="ghost" disabled={!options.length} onClick={copyRequest}>{copied ? <CheckIcon /> : <CopyIcon />}{copied ? "Copied" : "Copy cURL · model 1"}</Button></div>
          </CardContent>
        </Card>
      </form>
      <p className="text-xs leading-5 text-muted-foreground">First answer measures time from this browser&apos;s request to its first answer text, including network and authorization. Tokens/sec uses host-reported completion tokens divided by total elapsed time; it is not engine decode speed. Side-by-side requests share capacity, so these results are exploratory rather than a benchmark.</p>
      <div className="grid items-start gap-5 xl:grid-cols-2">{results.map((result, index) => <ComparisonResult key={index} result={result} index={index} available={options.length > 0} />)}</div>
    </div>
  )
}
