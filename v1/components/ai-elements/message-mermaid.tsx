"use client"

import * as React from "react"
import { useTheme } from "next-themes"
import { DownloadIcon, Maximize2Icon, RotateCcwIcon, ZoomInIcon, ZoomOutIcon } from "lucide-react"
import { StreamdownContext } from "streamdown"
import { toast } from "sonner"
import { CopyFeedbackIcon } from "./copy-feedback"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { useCopyFeedback } from "./use-copy-feedback"
import { MessageRenderAction, MessageRenderCard } from "./message-render-card"
import styles from "./message-mermaid.module.css"

type Diagram = { svg: string; width: number; height: number; chart: string; dark: boolean }

// Mermaid has global configuration. Serialize renders so simultaneous cards and
// theme changes cannot overwrite another diagram's configuration mid-render.
let renderQueue = Promise.resolve()
async function renderDiagram(chart: string, dark: boolean): Promise<Diagram> {
  let result!: Diagram
  const render = renderQueue.then(async () => {
    const { mermaid } = await import("@streamdown/mermaid")
    const text = dark ? "#ededed" : "#171717"
    const border = dark ? "#686868" : "#a3a3a3"
    const surface = dark ? "#252525" : "#f3f3f3"
    const background = dark ? "#171717" : "#ffffff"
    const instance = mermaid.getMermaid({
      theme: "base", darkMode: dark, securityLevel: "strict", startOnLoad: false,
      suppressErrorRendering: true, htmlLabels: false, fontFamily: "Geist, Arial, sans-serif",
      secure: ["securityLevel", "startOnLoad", "suppressErrorRendering", "maxTextSize", "maxEdges", "theme", "themeVariables", "htmlLabels"],
      themeVariables: {
        darkMode: dark, background, fontSize: "14px", primaryColor: surface, primaryTextColor: text,
        primaryBorderColor: border, secondaryColor: surface, secondaryTextColor: text,
        secondaryBorderColor: border, tertiaryColor: background, tertiaryTextColor: text,
        tertiaryBorderColor: border, lineColor: border, textColor: text, mainBkg: surface,
        nodeBorder: border, clusterBkg: background, clusterBorder: border,
        edgeLabelBackground: background, nodeTextColor: text, titleColor: text,
        actorBkg: surface, actorBorder: border, actorTextColor: text, signalColor: text,
        signalTextColor: text, labelBoxBkgColor: surface, labelBoxBorderColor: border,
        labelTextColor: text, noteBkgColor: surface, noteTextColor: text, noteBorderColor: border,
        activationBkgColor: surface, activationBorderColor: border,
      },
    })
    const { svg } = await instance.render(`fabric-diagram-${crypto.randomUUID()}`, chart)
    const document = new DOMParser().parseFromString(svg, "image/svg+xml")
    const root = document.documentElement
    const viewBox = root.getAttribute("viewBox")?.trim().split(/[\s,]+/).map(Number)
    if (root.localName !== "svg" || !viewBox || viewBox.length !== 4 || viewBox.some((value) => !Number.isFinite(value)) || viewBox[2] <= 0 || viewBox[3] <= 0) throw new Error("Diagram dimensions could not be read")
    root.setAttribute("width", String(viewBox[2]))
    root.setAttribute("height", String(viewBox[3]))
    root.setAttribute("style", "max-width:none;width:100%;height:100%;")
    root.setAttribute("preserveAspectRatio", "xMidYMid meet")
    result = { svg: new XMLSerializer().serializeToString(root), width: viewBox[2], height: viewBox[3], chart, dark }
  })
  renderQueue = render.catch(() => {})
  await render
  return result
}

function DiagramViewport({ diagram, expanded = false, showControls = true }: { diagram: Diagram; expanded?: boolean; showControls?: boolean }) {
  const viewportRef = React.useRef<HTMLDivElement>(null)
  const [width, setWidth] = React.useState(0)
  const [zoom, setZoom] = React.useState(1)
  React.useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [])
  const fit = width > 0 ? Math.min(Math.max(width - 32, 1) / diagram.width, 380 / diagram.height, 1) : 0
  // Keep labels readable on wide charts; the local scroller exposes every node.
  // Fit remains available when an overview is more useful than reading detail.
  const initialScale = Math.max(fit, .8)
  const scale = width ? initialScale * zoom : 0
  const minimumZoom = fit / initialScale
  const maximumZoom = 3 / initialScale
  function reset() { setZoom(minimumZoom); viewportRef.current?.scrollTo({ left: 0, top: 0 }) }
  return <div className={expanded ? styles.expanded : undefined}>
    <div ref={viewportRef} data-diagram-scroll className={styles.viewport} role="region" aria-label={expanded ? "Expanded diagram contents" : "Diagram contents"} tabIndex={0}>
      <div className={styles.stage} style={{ minHeight: expanded ? 320 : 160 }}>
        <div data-diagram-svg className={styles.diagram} style={{ width: diagram.width * scale, height: diagram.height * scale, visibility: width ? "visible" : "hidden" }} role="img" aria-label="Mermaid diagram" dangerouslySetInnerHTML={{ __html: diagram.svg }} />
      </div>
    </div>
    {showControls && <div data-slot="diagram-navigation" className={styles.navigation}>
      <MessageRenderAction label="Zoom out" disabled={zoom <= minimumZoom} onClick={() => setZoom((value) => Math.max(minimumZoom, value - .25))}><ZoomOutIcon className="size-4" /></MessageRenderAction>
      <span aria-live="polite" className={styles.zoom}>{Math.round(scale * 100)}%</span>
      <MessageRenderAction label="Zoom in" disabled={zoom >= maximumZoom} onClick={() => setZoom((value) => Math.min(maximumZoom, value + .25))}><ZoomInIcon className="size-4" /></MessageRenderAction>
      <MessageRenderAction label="Fit diagram" onClick={reset}><RotateCcwIcon className="size-4" /></MessageRenderAction>
    </div>}
  </div>
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function pngBlob(diagram: Diagram): Promise<Blob> {
  const root = new DOMParser().parseFromString(diagram.svg, "image/svg+xml").documentElement
  root.setAttribute("style", "max-width:none;")
  const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(root)], { type: "image/svg+xml;charset=utf-8" }))
  try {
    const image = new Image()
    image.src = url
    await image.decode()
    const canvas = document.createElement("canvas")
    const scale = Math.min(2, 4096 / Math.max(diagram.width, diagram.height))
    canvas.width = Math.ceil(diagram.width * scale)
    canvas.height = Math.ceil(diagram.height * scale)
    const context = canvas.getContext("2d")
    if (!context) throw new Error("Image export unavailable")
    context.drawImage(image, 0, 0, canvas.width, canvas.height)
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("Image export failed")), "image/png"))
  } finally { URL.revokeObjectURL(url) }
}

export function MessageMermaidBlock({ chart, incomplete }: { chart: string; incomplete: boolean }) {
  const { resolvedTheme } = useTheme()
  const dark = resolvedTheme === "dark"
  const { controls, isAnimating } = React.useContext(StreamdownContext)
  const mermaidControls = typeof controls === "object" ? controls.mermaid : controls
  const config = typeof mermaidControls === "object" ? mermaidControls : undefined
  const enabled = controls !== false && mermaidControls !== false
  const [diagram, setDiagram] = React.useState<Diagram | null>(null)
  const [failure, setFailure] = React.useState<{ chart: string; message: string } | null>(null)
  const [retry, setRetry] = React.useState(0)
  const [expanded, setExpanded] = React.useState(false)
  const expandRef = React.useRef<HTMLButtonElement>(null)
  const { copied, copy } = useCopyFeedback()
  React.useEffect(() => {
    if (incomplete || !chart.trim()) return
    let cancelled = false
    const timer = setTimeout(() => {
      renderDiagram(chart, dark).then((rendered) => { if (!cancelled) { setDiagram(rendered); setFailure(null) } }).catch((error: unknown) => {
        if (!cancelled) setFailure({ chart, message: error instanceof Error ? error.message : "Unable to render diagram" })
      })
    }, 150)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [chart, dark, incomplete, retry])
  const current = diagram?.chart === chart && diagram.dark === dark
  const failed = failure?.chart === chart && !incomplete && !isAnimating
  const disabled = incomplete || isAnimating
  async function copyDiagram() {
    if (disabled) return
    if (await copy(chart)) { if (typeof config?.copy === "object") config.copy.onCopy?.() }
    else { if (typeof config?.copy === "object") config.copy.onError?.(new Error("Unable to copy diagram")); toast.error("Unable to copy diagram") }
  }
  async function download(format: "svg" | "png" | "mmd") {
    if (disabled) return
    try {
      const name = typeof config?.download === "object" ? config.download.filename.replace(/\.(svg|png|mmd)$/i, "") : "diagram"
      if (format === "mmd") downloadBlob(new Blob([chart], { type: "text/plain;charset=utf-8" }), `${name}.mmd`)
      else if (diagram && current) downloadBlob(format === "png" ? await pngBlob(diagram) : new Blob([diagram.svg], { type: "image/svg+xml;charset=utf-8" }), `${name}.${format}`)
    } catch { toast.error("Unable to download diagram") }
  }
  return <>
    <MessageRenderCard data-slot="message-diagram" title="Diagram" actions={enabled && <>
      {config?.copy !== false && <MessageRenderAction label={copied ? "Copied!" : "Copy diagram source"} disabled={disabled} onClick={copyDiagram}><CopyFeedbackIcon copied={copied} className="size-4" /><span className="sr-only" role="status">{copied ? "Copied to clipboard" : ""}</span></MessageRenderAction>}
      {config?.download !== false && <DropdownMenu><DropdownMenuTrigger render={<MessageRenderAction label="Download diagram" disabled={disabled}><DownloadIcon className="size-4" /></MessageRenderAction>} /><DropdownMenuContent align="end">
        <DropdownMenuItem disabled={!current} onClick={() => download("svg")}>Download SVG</DropdownMenuItem>
        <DropdownMenuItem disabled={!current} onClick={() => download("png")}>Download PNG</DropdownMenuItem>
        <DropdownMenuItem onClick={() => download("mmd")}>Download source</DropdownMenuItem>
      </DropdownMenuContent></DropdownMenu>}
      {config?.fullscreen !== false && <MessageRenderAction ref={expandRef} label="Expand diagram" disabled={!current} onClick={() => setExpanded(true)}><Maximize2Icon className="size-4" /></MessageRenderAction>}
    </>}>
      {failed ? <div role="alert" className={styles.error}><p>Unable to render this diagram.</p><details><summary>View source</summary><pre>{chart}</pre></details><button className={styles.retry} type="button" onClick={() => setRetry((value) => value + 1)}>Try again</button></div>
        : diagram && current ? <DiagramViewport key={diagram.svg} diagram={diagram} showControls={enabled && config?.panZoom !== false} />
        : <div role="status" className={styles.loading}>{incomplete ? "Waiting for diagram…" : "Rendering diagram…"}</div>}
    </MessageRenderCard>
    <Dialog open={expanded} onOpenChange={setExpanded}><DialogContent finalFocus={expandRef} className="flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[min(1200px,calc(100%-2rem))]">
      <div className="shrink-0 border-b border-border/40 px-4 py-4 pr-12"><DialogTitle className="text-sm">Diagram</DialogTitle><DialogDescription className="sr-only">Expanded diagram. Zoom or scroll to view its details.</DialogDescription></div>
      {diagram && current && <DiagramViewport key={diagram.svg} diagram={diagram} expanded showControls={enabled && config?.panZoom !== false} />}
    </DialogContent></Dialog>
  </>
}
