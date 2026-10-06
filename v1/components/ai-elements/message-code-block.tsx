"use client"

import * as React from "react"
import { DownloadIcon, Maximize2Icon } from "lucide-react"
import { StreamdownContext, defaultComponents, type ControlsConfig, type ExtraProps, useIsCodeFenceIncomplete } from "streamdown"
import { toast } from "sonner"
import { CopyFeedbackIcon } from "./copy-feedback"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { useCopyFeedback } from "./use-copy-feedback"
import { MessageRenderAction, MessageRenderCard } from "./message-render-card"
import { MessageMermaidBlock } from "./message-mermaid"

type MarkdownCodeProps = React.ComponentProps<"code"> & ExtraProps & {
  "data-block"?: string
}

// Fullscreen is implemented by this renderer; Streamdown's built-in code
// controls declare only copy/download. Keep their types and add our option.
type MessageCodeControls = Exclude<Exclude<ControlsConfig, boolean>["code"], boolean | undefined> & {
  fullscreen?: boolean
}

const LANGUAGE_LABELS: Record<string, string> = {
  bash: "Bash", sh: "Bash", shell: "Bash", zsh: "Bash",
  yaml: "YAML", yml: "YAML", python: "Python", py: "Python",
  javascript: "JavaScript", js: "JavaScript", typescript: "TypeScript", ts: "TypeScript",
  jsx: "JSX", tsx: "TSX", json: "JSON", html: "HTML", css: "CSS", sql: "SQL",
  go: "Go", rust: "Rust", java: "Java", c: "C", cpp: "C++", csharp: "C#",
  markdown: "Markdown", md: "Markdown", plaintext: "Text", text: "Text",
}

const LANGUAGE_EXTENSIONS: Record<string, string> = {
  bash: "sh", shell: "sh", zsh: "sh", yaml: "yaml", yml: "yaml", python: "py",
  javascript: "js", typescript: "ts", csharp: "cs", cpp: "cpp", markdown: "md",
  plaintext: "txt", text: "txt",
}

function CodeContents({ code, expanded = false }: { code: string; expanded?: boolean }) {
  return (
    <div data-streamdown="code-block-body" role="region" aria-label={expanded ? "Expanded code contents" : "Code contents"} tabIndex={0} className={`w-full min-w-0 overflow-auto p-4 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring/40 ${expanded ? "min-h-0 flex-1" : ""}`}>
      <pre className="m-0! w-full font-mono text-[13px] leading-5 whitespace-pre"><code className="block bg-transparent font-mono text-inherit">{code.replace(/\n$/, "")}</code></pre>
    </div>
  )
}

function textContent(children: React.ReactNode): string {
  return React.Children.toArray(children).map((child) => {
    if (typeof child === "string" || typeof child === "number") return String(child)
    if (React.isValidElement<{ children?: React.ReactNode }>(child)) return textContent(child.props.children)
    return ""
  }).join("")
}

function MessageCodeBlock({ code, language, incomplete }: { code: string; language: string; incomplete: boolean }) {
  const { copied, copy } = useCopyFeedback()
  const { controls, isAnimating } = React.useContext(StreamdownContext)
  const [expanded, setExpanded] = React.useState(false)
  const expandRef = React.useRef<HTMLButtonElement>(null)
  const codeControls = typeof controls === "object" ? controls.code : controls
  const config: MessageCodeControls | undefined = typeof codeControls === "object" ? codeControls : undefined
  const enabled = controls !== false && codeControls !== false
  const disabled = incomplete || isAnimating
  const label = LANGUAGE_LABELS[language.toLowerCase()] ?? (language ? language[0].toUpperCase() + language.slice(1) : "Code")

  async function copyCode() {
    if (disabled) return
    if (await copy(code)) {
      if (typeof config?.copy === "object") config.copy.onCopy?.()
    } else {
      if (typeof config?.copy === "object") config.copy.onError?.(new Error("Unable to copy code"))
      toast.error("Unable to copy code")
    }
  }

  function downloadCode() {
    if (disabled) return
    try {
      const url = URL.createObjectURL(new Blob([code], { type: "text/plain;charset=utf-8" }))
      const link = document.createElement("a")
      const extension = LANGUAGE_EXTENSIONS[language.toLowerCase()] ?? (/^[a-z0-9]{1,10}$/i.test(language) ? language.toLowerCase() : "txt")
      link.href = url
      link.download = typeof config?.download === "object" ? config.download.filename : `code.${extension}`
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { toast.error("Unable to download code") }
  }

  return (
    <>
    <MessageRenderCard data-slot="message-code-block" data-streamdown="code-block" data-language={language} data-incomplete={incomplete || undefined} dir="ltr" title={label} actions={enabled && <>
        {config?.copy !== false && <MessageRenderAction label={copied ? "Copied!" : "Copy code"} disabled={disabled} onClick={copyCode}>
          <CopyFeedbackIcon copied={copied} className="size-4" />
          <span className="sr-only" role="status">{copied ? "Copied to clipboard" : ""}</span>
        </MessageRenderAction>}
        {config?.download !== false && <MessageRenderAction label="Download code" disabled={disabled} onClick={downloadCode}><DownloadIcon className="size-4" /></MessageRenderAction>}
        {config?.fullscreen !== false && <MessageRenderAction label="Expand code" ref={expandRef} onClick={() => setExpanded(true)}><Maximize2Icon className="size-4" /></MessageRenderAction>}
        </>}>
      <CodeContents code={code} />
    </MessageRenderCard>
    <Dialog open={expanded} onOpenChange={setExpanded}>
      <DialogContent finalFocus={expandRef} className="flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[min(1200px,calc(100%-2rem))]">
        <div className="shrink-0 border-b border-border/40 px-4 py-4 pr-12"><DialogTitle className="text-sm">{label} code</DialogTitle><DialogDescription className="sr-only">Expanded code block. Scroll to read the full source.</DialogDescription></div>
        <CodeContents code={code} expanded />
      </DialogContent>
    </Dialog>
    </>
  )
}

/** Keep inline code semantic; fenced code and diagrams share the message card. */
function MessageMarkdownCode({ children, className, node, ...props }: MarkdownCodeProps) {
  const incomplete = useIsCodeFenceIncomplete()
  const language = /(?:^|\s)language-([^\s]+)/.exec(className ?? "")?.[1] ?? ""
  const block = "data-block" in props
  if (!block) {
    const DefaultCode = defaultComponents.code
    return <DefaultCode className={className} node={node} {...props}>{children}</DefaultCode>
  }

  if (language.toLowerCase() === "mermaid") return <MessageMermaidBlock chart={textContent(children)} incomplete={incomplete} />

  return <MessageCodeBlock code={textContent(children)} language={language} incomplete={incomplete} />
}

export { MessageMarkdownCode }
