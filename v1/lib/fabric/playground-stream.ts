export type StreamUsage = { input: number; output: number }
export type StreamUpdate = {
  content: string
  reasoning: string
  usage: StreamUsage | null
  done: boolean
  finishReason: string | null
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function tokenCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
}

// SSE chunk boundaries do not coincide with frame boundaries, including CRLF and
// UTF-8. Decode bytes before feeding this parser; retain only the incomplete frame.
export class PlaygroundStreamParser {
  private buffer = ""
  private ended = false

  feed(text: string): StreamUpdate[] {
    if (this.ended) return []
    this.buffer += text
    if (this.buffer.length > 1_000_000) throw new Error("Inference frame exceeded the supported size.")
    const updates: StreamUpdate[] = []
    let boundary: RegExpExecArray | null
    while ((boundary = /\r?\n\r?\n/.exec(this.buffer)) !== null) {
      const frame = this.buffer.slice(0, boundary.index)
      this.buffer = this.buffer.slice(boundary.index + boundary[0].length)
      const data = frame.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).replace(/^ /, "")).join("\n")
      if (!data) continue
      if (data === "[DONE]") {
        this.ended = true
        updates.push({ content: "", reasoning: "", usage: null, done: true, finishReason: null })
        this.buffer = ""
        break
      }
      let raw: unknown
      try { raw = JSON.parse(data) } catch { throw new Error("Inference returned an invalid stream frame.") }
      const payload = object(raw)
      if (!payload) throw new Error("Inference returned an invalid stream frame.")
      if (payload.error) throw new Error("The inference gateway reported an error while streaming.")
      const choices = Array.isArray(payload.choices) ? payload.choices : []
      const choice = object(choices[0])
      const delta = object(choice?.delta)
      const usage = object(payload.usage)
      const terminalUsage = choices.length === 0 && usage && tokenCount(usage.prompt_tokens) && tokenCount(usage.completion_tokens)
        ? { input: usage.prompt_tokens, output: usage.completion_tokens } : null
      updates.push({
        content: typeof delta?.content === "string" ? delta.content : "",
        reasoning: typeof delta?.reasoning_content === "string" ? delta.reasoning_content : typeof delta?.reasoning === "string" ? delta.reasoning : "",
        usage: terminalUsage, done: false,
        finishReason: typeof choice?.finish_reason === "string" ? choice.finish_reason : null,
      })
    }
    return updates
  }

  finish() {
    if (!this.ended) throw new Error("Inference stream ended before its completion marker.")
  }
}

export function completionTokensPerSecond(usage: StreamUsage | null, elapsedMs: number, complete: boolean) {
  return complete && usage && elapsedMs > 0 ? usage.output / (elapsedMs / 1000) : null
}
