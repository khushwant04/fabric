"use client"

import { useEffect, useMemo, useRef, useState, useTransition } from "react"
import { AlertTriangleIcon, CheckIcon, HistoryIcon, PlusIcon, RotateCcwIcon, SaveIcon, Trash2Icon } from "lucide-react"

import { PageHeader } from "@/components/console/page-header"
import { Section } from "@/components/console/section"
import { Status } from "@/components/hexel/status"
import { PolicyFlow, PolicyFlowList } from "@/components/routing/policy-flow"
import { PublishDialog } from "@/components/routing/publish-dialog"
import { RulePanel, type Named } from "@/components/routing/rule-panel"
import { TestPanel } from "@/components/routing/test-panel"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatRelative } from "@/lib/format"
import {
  deleteRoutingPolicy,
  listRoutingVersions,
  publishRoutingPolicy,
  rollbackRoutingPolicy,
  saveRoutingDraft,
  validateRoutingDraft,
} from "@/lib/fabric/routing-actions"
import {
  flowLayout,
  MAX_RULES,
  moveItem,
  newRule,
  sameDocument,
  type DeploymentOption,
  type FlowNode,
  type PolicyDocument,
  type PolicyVersion,
  type RoutingPolicy,
  type RoutingRule,
  type RuleAction,
  type ValidationReport,
} from "@/lib/fabric/routing-policy"

export function PolicyEditor({
  initial,
  versions: initialVersions,
  deployments,
  apiKeys,
  principals,
  stamps,
  canWrite,
  initialTab = "test",
}: {
  initial: RoutingPolicy
  versions: PolicyVersion[]
  deployments: DeploymentOption[]
  apiKeys: Named[]
  principals: Named[]
  stamps: Named[]
  canWrite: boolean
  initialTab?: string
}) {
  const [policy, setPolicy] = useState(initial)
  const [draft, setDraft] = useState<PolicyDocument>(initial.draft)
  const [report, setReport] = useState<ValidationReport>(initial.validation)
  const [versions, setVersions] = useState(initialVersions)
  const [selected, setSelected] = useState(initial.draft.rules[0]?.id ?? initial.draft.default.id)
  const [tab, setTab] = useState(initialTab)
  const [message, setMessage] = useState<{ tone: "good" | "error"; text: string } | null>(null)
  const [pending, startTransition] = useTransition()
  const [validating, setValidating] = useState(false)
  const firstRender = useRef(true)

  const dirty = !sameDocument(draft, policy.draft)
  const differsFromLive = !policy.live || !sameDocument(draft, policy.live.document)
  const names = useMemo(() => new Map(deployments.map((item) => [item.id, item.name])), [deployments])
  const issues = useMemo(() => [...report.errors, ...report.warnings], [report])
  const order = useMemo(
    () => flowLayout(policy.alias, policy.live_version, draft, deployments).nodes
      .filter((node) => node.kind === "target")
      .map((node) => (node as Extract<FlowNode, { kind: "target" }>).deploymentId),
    [policy.alias, policy.live_version, draft, deployments]
  )

  // Re-validate edits against the account (owned deployments, readiness) without saving.
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return }
    setValidating(true)
    const timer = setTimeout(async () => {
      const outcome = await validateRoutingDraft(policy.id, draft)
      if (outcome.ok) setReport(outcome.value)
      setValidating(false)
    }, 500)
    return () => clearTimeout(timer)
  }, [draft, policy.id])

  const isDefault = selected === draft.default.id
  const ruleIndex = draft.rules.findIndex((rule) => rule.id === selected)
  const current: RuleAction | RoutingRule = isDefault || ruleIndex < 0 ? draft.default : draft.rules[ruleIndex]

  const updateRule = (rule: RuleAction | RoutingRule) => {
    setMessage(null)
    if (isDefault || ruleIndex < 0) setDraft({ ...draft, default: rule as RuleAction })
    else setDraft({ ...draft, rules: draft.rules.map((item, index) => (index === ruleIndex ? (rule as RoutingRule) : item)) })
  }

  const addRule = () => {
    const rule = newRule([...draft.rules.map((item) => item.id), draft.default.id], draft.default.targets)
    setDraft({ ...draft, rules: [...draft.rules, rule] })
    setSelected(rule.id)
  }

  const apply = (next: RoutingPolicy, text: string) => {
    setPolicy(next)
    setDraft(next.draft)
    setReport(next.validation)
    setMessage({ tone: "good", text })
  }

  const save = () => startTransition(async () => {
    const outcome = await saveRoutingDraft(policy.id, draft)
    if (outcome.ok) apply(outcome.value, "Draft saved. Live traffic is unchanged until you publish.")
    else setMessage({ tone: "error", text: outcome.error })
  })

  const refreshVersions = async () => {
    const outcome = await listRoutingVersions(policy.id)
    if (outcome.ok) setVersions(outcome.value)
  }

  const publish = async (note: string) => {
    let succeeded = false
    await new Promise<void>((resolve) => startTransition(async () => {
      const outcome = await publishRoutingPolicy(policy.id, draft, note)
      if (outcome.ok) {
        apply(outcome.value, `Version ${outcome.value.live_version} is live.`)
        await refreshVersions()
        succeeded = true
      } else {
        setMessage({ tone: "error", text: outcome.issues?.length ? `${outcome.error}: ${outcome.issues.map((issue) => issue.message).join(" ")}` : outcome.error })
      }
      resolve()
    }))
    return succeeded
  }

  const rollback = (version: number) => startTransition(async () => {
    const outcome = await rollbackRoutingPolicy(policy.id, version)
    if (outcome.ok) {
      apply(outcome.value, `Version ${version} was republished as version ${outcome.value.live_version}.`)
      await refreshVersions()
    } else {
      setMessage({ tone: "error", text: outcome.error })
    }
  })

  const status = policy.live_version
    ? <Status tone={differsFromLive ? "warning" : "good"}>Live v{policy.live_version}{differsFromLive ? " · draft differs" : ""}</Status>
    : <Status tone="neutral">Not published</Status>

  return <>
    <PageHeader
      title={policy.alias}
      description={policy.description || "Requests for this model name are matched against the rules in order; the first match decides where they go."}
      actions={<>
        <span className="mr-1 hidden sm:inline-flex">{status}</span>
        <Button variant="outline" disabled={!canWrite || !dirty || pending} onClick={save}>{pending ? <Spinner /> : <SaveIcon />}Save draft</Button>
        <PublishDialog live={policy.live?.document ?? null} liveVersion={policy.live_version} draft={draft} names={names} errors={report.errors} warnings={report.warnings} disabled={!canWrite || !differsFromLive || pending} pending={pending} onPublish={publish} />
      </>}
    >
      <span className="sm:hidden">{status}</span>
    </PageHeader>

    {message ? <Alert variant={message.tone === "error" ? "destructive" : "default"}>{message.tone === "error" ? <AlertTriangleIcon /> : <CheckIcon />}<AlertTitle>{message.tone === "error" ? "Not saved" : "Done"}</AlertTitle><AlertDescription>{message.text}</AlertDescription></Alert> : null}
    {report.errors.length ? <Alert variant="destructive"><AlertTriangleIcon /><AlertTitle>{report.errors.length} problem{report.errors.length === 1 ? "" : "s"} to fix before publishing</AlertTitle><AlertDescription><ul className="list-disc pl-4">{report.errors.slice(0, 4).map((issue, index) => <li key={index}>{issue.message}</li>)}</ul></AlertDescription></Alert> : null}

    <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_420px]">
      <Section
        title="Flow"
        description={`${draft.rules.length + 1} rule${draft.rules.length ? "s" : ""} · first match wins${validating ? " · checking…" : ""}`}
        action={<Button variant="outline" size="sm" disabled={!canWrite || draft.rules.length >= MAX_RULES} onClick={addRule}><PlusIcon /> Add rule</Button>}
      >
        <div className="hidden border-t border-border md:block"><PolicyFlow alias={policy.alias} version={policy.live_version} document={draft} deployments={deployments} issues={issues} selected={selected} onSelect={setSelected} /></div>
        <div className="border-t border-border p-4 md:hidden"><PolicyFlowList document={draft} deployments={deployments} selected={selected} onSelect={setSelected} /></div>
      </Section>

      <Section title={isDefault ? "Default rule" : current.name || "Rule"} description={isDefault ? "Serves every request no rule above matched." : "Select a rule in the flow to edit it."}>
        <RulePanel
          rule={current}
          isDefault={isDefault}
          position={ruleIndex + 1}
          ruleCount={draft.rules.length}
          deployments={deployments}
          order={order}
          apiKeys={apiKeys}
          principals={principals}
          stamps={stamps}
          issues={issues.filter((issue) => issue.rule_id === current.id)}
          readOnly={!canWrite}
          onChange={updateRule}
          onMove={(delta) => setDraft({ ...draft, rules: moveItem(draft.rules, ruleIndex, ruleIndex + delta) })}
          onDelete={() => { setDraft({ ...draft, rules: draft.rules.filter((rule) => rule.id !== current.id) }); setSelected(draft.default.id) }}
        />
      </Section>
    </div>

    <Section title="Test, history and checks" className="min-w-0">
      <Tabs value={tab} onValueChange={(value) => setTab(String(value))} className="gap-0">
        <div className="overflow-x-auto px-6"><TabsList variant="line">
          <TabsTrigger value="test">Test a request</TabsTrigger>
          <TabsTrigger value="versions">Versions ({versions.length})</TabsTrigger>
          <TabsTrigger value="checks">Checks ({report.errors.length + report.warnings.length})</TabsTrigger>
        </TabsList></div>
        <TabsContent value="test" className="pt-5">
          <TestPanel policyId={policy.id} document={draft} hasLive={Boolean(policy.live_version)} apiKeys={apiKeys} principals={principals} order={order} onMatched={setSelected} />
        </TabsContent>
        <TabsContent value="versions" className="pt-2">
          {versions.length ? <ul className="flex flex-col divide-y divide-border border-t border-border">{versions.map((version) => <li key={version.version} className="flex flex-wrap items-center justify-between gap-3 px-6 py-3.5">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-sm font-medium">Version {version.version}{version.version === policy.live_version ? <Status tone="good">Live</Status> : null}{version.rolled_back_from ? <span className="text-xs font-normal text-muted-foreground">restores v{version.rolled_back_from}</span> : null}</div>
              <div className="text-xs text-muted-foreground">{formatRelative(version.published_at)}{version.note ? ` · ${version.note}` : ""} · {version.document.rules.length + 1} rules</div>
            </div>
            {version.version !== policy.live_version ? <AlertDialog>
              <AlertDialogTrigger render={<Button variant="outline" size="sm" disabled={!canWrite || pending}><RotateCcwIcon /> Roll back</Button>} />
              <AlertDialogContent>
                <AlertDialogHeader><AlertDialogTitle>Republish version {version.version}?</AlertDialogTitle><AlertDialogDescription>It becomes version {(versions[0]?.version ?? 0) + 1} and goes live. The draft is replaced with it; history is kept.</AlertDialogDescription></AlertDialogHeader>
                <AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={() => rollback(version.version)}>Roll back</AlertDialogAction></AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog> : null}
          </li>)}</ul> : <p className="flex items-center gap-2 border-t border-border px-6 py-10 text-sm text-muted-foreground"><HistoryIcon className="size-4" />Nothing published yet. Each publish keeps an immutable version you can roll back to.</p>}
        </TabsContent>
        <TabsContent value="checks" className="pt-2">
          {issues.length ? <ul className="flex flex-col divide-y divide-border border-t border-border">{issues.map((issue, index) => <li key={index} className="flex items-start gap-3 px-6 py-3 text-sm">
            <Status tone={issue.severity === "error" ? "critical" : "warning"}>{issue.severity === "error" ? "Error" : "Warning"}</Status>
            <button type="button" className="min-w-0 text-left hover:underline disabled:no-underline" disabled={!issue.rule_id} onClick={() => issue.rule_id && setSelected(issue.rule_id)}>{issue.message}</button>
          </li>)}</ul> : <p className="flex items-center gap-2 border-t border-border px-6 py-10 text-sm text-muted-foreground"><CheckIcon className="size-4" />No problems found.</p>}
        </TabsContent>
      </Tabs>
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-6 py-3 text-xs text-muted-foreground">
        <span>Draft saved {formatRelative(policy.draft_updated_at)}{dirty ? " · unsaved edits" : ""}</span>
        <AlertDialog>
          <AlertDialogTrigger render={<Button variant="ghost" size="sm" disabled={!canWrite || pending}><Trash2Icon /> Delete policy</Button>} />
          <AlertDialogContent>
            <AlertDialogHeader><AlertDialogTitle>Delete {policy.alias}?</AlertDialogTitle><AlertDialogDescription>Stamps and the edge stop routing this name on their next pull, and requests for it get “model not found”. Version history is kept in the audit log.</AlertDialogDescription></AlertDialogHeader>
            <AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction variant="destructive" onClick={() => startTransition(async () => { const outcome = await deleteRoutingPolicy(policy.id); if (outcome && !outcome.ok) setMessage({ tone: "error", text: outcome.error }) })}>Delete</AlertDialogAction></AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </Section>
  </>
}
