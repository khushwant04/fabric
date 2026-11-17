"use client"

import { useMemo } from "react"
import { useTheme } from "next-themes"
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react"
import { AlertTriangleIcon, BoxIcon, CircleDotIcon, RouteIcon } from "lucide-react"

import "@xyflow/react/dist/style.css"

import { cn } from "@/lib/utils"
import { FLOW, flowLayout, type DeploymentOption, type FlowNode, type PolicyDocument, type ValidationIssue } from "@/lib/fabric/routing-policy"

/** Series colours, one per deployment, in the order deployments first appear. */
export const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"]

export function seriesColor(deploymentIds: string[], id: string) {
  const index = deploymentIds.indexOf(id)
  return SERIES[(index < 0 ? 0 : index) % SERIES.length]
}

type Data = { node: FlowNode; selected: boolean; color?: string }

function AliasNode({ data }: NodeProps<Node<Data>>) {
  const node = data.node as Extract<FlowNode, { kind: "alias" }>
  return <div className="flex w-[220px] items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-[0_1px_2px_rgb(0_0_0/0.04)]">
    <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-foreground"><RouteIcon className="size-4" /></span>
    <div className="min-w-0">
      <div className="text-[11px] font-medium tracking-[0.08em] text-muted-foreground uppercase">Model name</div>
      <div className="truncate font-mono text-[13px] font-medium">{node.label}</div>
      <div className="text-xs text-muted-foreground">{node.version ? `Live v${node.version}` : "Not published"}</div>
    </div>
    <Handle type="source" position={Position.Right} className="!size-2 !border-0 !bg-muted-foreground/60" />
  </div>
}

function RuleNode({ data }: NodeProps<Node<Data>>) {
  const node = data.node as Extract<FlowNode, { kind: "rule" }>
  return <div className={cn(
    "w-[300px] cursor-pointer rounded-xl border bg-card px-4 py-3 shadow-[0_1px_2px_rgb(0_0_0/0.04)] transition-colors",
    data.selected ? "border-foreground ring-2 ring-ring/30" : "border-border hover:border-foreground/40"
  )}>
    <Handle type="target" position={Position.Left} className="!size-2 !border-0 !bg-muted-foreground/60" />
    <div className="flex items-center justify-between gap-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className="flex h-5 min-w-5 items-center justify-center rounded-md bg-muted px-1 text-[11px] font-medium tabular-nums">{node.isDefault ? "∗" : node.index}</span>
        <span className="truncate text-sm font-medium">{node.label}</span>
      </div>
      {node.issues ? <span className="flex items-center gap-1 text-xs text-status-warning"><AlertTriangleIcon className="size-3.5" />{node.issues}</span> : null}
    </div>
    <div className="mt-1.5 line-clamp-2 text-xs text-muted-foreground">
      {node.isDefault ? "Every request no rule above matched" : node.conditions.length ? node.conditions.join(" · ") : "Every request (no conditions)"}
    </div>
    <Handle type="source" position={Position.Right} className="!size-2 !border-0 !bg-muted-foreground/60" />
  </div>
}

function TargetNode({ data }: NodeProps<Node<Data>>) {
  const node = data.node as Extract<FlowNode, { kind: "target" }>
  return <div className={cn("w-[240px] rounded-xl border bg-card px-4 py-3 shadow-[0_1px_2px_rgb(0_0_0/0.04)]", node.known ? "border-border" : "border-destructive/60")}>
    <Handle type="target" position={Position.Left} className="!size-2 !border-0 !bg-muted-foreground/60" />
    <div className="flex items-center gap-2">
      <span className="size-2.5 shrink-0 rounded-full" style={{ background: data.color }} aria-hidden="true" />
      <BoxIcon className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate text-sm font-medium">{node.label}</span>
    </div>
    <div className="mt-1 flex items-center justify-between gap-2 text-xs text-muted-foreground">
      <span className="truncate font-mono">{node.alias}</span>
      <span className="flex shrink-0 items-center gap-1"><CircleDotIcon className={cn("size-3", node.ready ? "text-status-good" : "text-muted-foreground/60")} />{node.known ? (node.ready ? "Ready" : "Not ready") : "Unknown"}</span>
    </div>
  </div>
}

const nodeTypes = { alias: AliasNode, rule: RuleNode, target: TargetNode }

export function PolicyFlow({
  alias,
  version,
  document,
  deployments,
  issues,
  selected,
  onSelect,
}: {
  alias: string
  version: number | null
  document: PolicyDocument
  deployments: DeploymentOption[]
  issues: ValidationIssue[]
  selected: string
  onSelect: (ruleId: string) => void
}) {
  const { resolvedTheme } = useTheme()
  const layout = useMemo(() => flowLayout(alias, version, document, deployments, issues), [alias, version, document, deployments, issues])
  const order = layout.nodes.filter((node) => node.kind === "target").map((node) => (node as Extract<FlowNode, { kind: "target" }>).deploymentId)

  const nodes: Node<Data>[] = layout.nodes.map((node) => ({
    id: node.id,
    type: node.kind,
    position: { x: node.x, y: node.y },
    data: {
      node,
      selected: node.kind === "rule" && node.ruleId === selected,
      color: node.kind === "target" ? seriesColor(order, node.deploymentId) : undefined,
    },
    draggable: false,
    connectable: false,
    selectable: node.kind === "rule",
  }))
  const edges: Edge[] = layout.edges.map((edge) => {
    const active = edge.ruleId === selected
    const color = edge.kind === "split" ? seriesColor(order, edge.target.slice(4)) : "var(--muted-foreground)"
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: edge.kind === "rule" ? "smoothstep" : "default",
      label: edge.kind === "rule" ? undefined : edge.label,
      animated: false,
      style: {
        stroke: color,
        strokeWidth: active ? 2.25 : 1.25,
        strokeDasharray: edge.kind === "fallback" ? "5 4" : undefined,
        opacity: selected && !active && edge.kind !== "rule" ? 0.35 : 1,
      },
      markerEnd: edge.kind === "rule" ? undefined : { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
      labelStyle: { fill: "var(--foreground)", fontSize: 11, fontWeight: 500 },
      labelBgStyle: { fill: "var(--card)", stroke: "var(--border)" },
      labelBgPadding: [6, 3] as [number, number],
      labelBgBorderRadius: 6,
      zIndex: active ? 10 : 0,
    }
  })
  const height = Math.max(380, Math.min(720, layout.nodes.filter((node) => node.kind === "rule").length * FLOW.ruleHeight + 120))

  return <div className="routing-flow w-full" style={{ height }} aria-label="Routing flow">
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      colorMode={resolvedTheme === "dark" ? "dark" : "light"}
      fitView
      fitViewOptions={{ padding: 0.12 }}
      minZoom={0.3}
      maxZoom={1.5}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      panOnScroll
      zoomOnScroll={false}
      proOptions={{ hideAttribution: true }}
      onNodeClick={(_, node) => {
        const data = node.data as Data
        if (data.node.kind === "rule") onSelect(data.node.ruleId)
      }}
      style={{ background: "transparent" }}
    >
      <Background gap={20} size={1} color="var(--border)" />
      <Controls showInteractive={false} position="bottom-right" />
    </ReactFlow>
  </div>
}

/** The same flow as a readable list, for narrow screens. */
export function PolicyFlowList({
  document,
  deployments,
  selected,
  onSelect,
}: {
  document: PolicyDocument
  deployments: DeploymentOption[]
  selected: string
  onSelect: (ruleId: string) => void
}) {
  const layout = flowLayout("", null, document, deployments)
  const names = new Map(deployments.map((item) => [item.id, item.name]))
  const order = layout.nodes.filter((node) => node.kind === "target").map((node) => (node as Extract<FlowNode, { kind: "target" }>).deploymentId)
  const rules = [...document.rules.map((rule, index) => ({ rule, index: index + 1, isDefault: false })), { rule: document.default, index: 0, isDefault: true }]
  return <ol className="flex flex-col gap-2">
    {rules.map(({ rule, index, isDefault }) => {
      const node = layout.nodes.find((item) => item.id === `rule:${rule.id}`) as Extract<FlowNode, { kind: "rule" }>
      return <li key={rule.id}>
        <button type="button" onClick={() => onSelect(rule.id)} className={cn("w-full rounded-xl border bg-card px-4 py-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring", selected === rule.id ? "border-foreground" : "border-border")}>
          <div className="flex items-center gap-2 text-sm font-medium"><span className="flex h-5 min-w-5 items-center justify-center rounded-md bg-muted px-1 text-[11px] tabular-nums">{isDefault ? "∗" : index}</span><span className="truncate">{rule.name || rule.id}</span></div>
          <div className="mt-1 text-xs text-muted-foreground">{isDefault ? "Every request no rule above matched" : node.conditions.join(" · ") || "Every request"}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {rule.targets.map((target) => <span key={target.deployment_id} className="inline-flex items-center gap-1.5 rounded-md bg-muted px-1.5 py-0.5 text-xs"><span className="size-2 rounded-full" style={{ background: seriesColor(order, target.deployment_id) }} />{names.get(target.deployment_id) ?? "Unknown"} <span className="tabular-nums text-muted-foreground">{target.weight}%</span></span>)}
            {rule.fallbacks.map((id, position) => <span key={id} className="inline-flex items-center gap-1 rounded-md border border-dashed border-border px-1.5 py-0.5 text-xs text-muted-foreground">fallback {position + 1}: {names.get(id) ?? "Unknown"}</span>)}
          </div>
        </button>
      </li>
    })}
  </ol>
}
