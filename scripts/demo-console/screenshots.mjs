// Capture every console page from a running `make demo-console`.
//
// Demo tooling only. Signs in through the mock identity provider exactly as a person would
// (the console's /auth/login, the provider's /authorize, the console's callback), then
// visits each page at 1440x900, device scale factor 2.
//
// Environment (set by scripts/demo-console/run.sh with DEMO_SCREENSHOTS=1):
//   DEMO_CONSOLE_URL  console origin, e.g. http://localhost:3000
//   DEMO_STATE        state.json written by seed.py
//   DEMO_OUT          output directory for PNGs
//   DEMO_ONLY         optional comma-separated shot names to capture

import { createRequire } from "node:module"
import { mkdirSync, readFileSync } from "node:fs"
import path from "node:path"

const require = createRequire(import.meta.url)
let playwright
try {
  playwright = require("playwright")
} catch {
  console.error("Playwright is not installed. Install it with: npm install -g playwright && npx playwright install chromium")
  process.exit(1)
}

const base = process.env.DEMO_CONSOLE_URL || "http://localhost:3000"
const state = JSON.parse(readFileSync(process.env.DEMO_STATE, "utf8"))
const out = process.env.DEMO_OUT
const only = new Set((process.env.DEMO_ONLY || "").split(",").map((item) => item.trim()).filter(Boolean))
mkdirSync(out, { recursive: true })

// Next's development-mode badge is not part of the product.
const HIDE_DEV_CHROME = "nextjs-portal, [data-nextjs-toast], #__next-build-watcher { display: none !important; }"
const LOADING = "[data-slot='skeleton'], .animate-pulse, .animate-spin, [aria-busy='true']"

async function settle(page) {
  await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {})
  await page.waitForFunction((selector) => {
    const visible = [...document.querySelectorAll(selector)].filter((element) => {
      const box = element.getBoundingClientRect()
      return box.width > 0 && box.height > 0
    })
    return visible.length === 0
  }, LOADING, { timeout: 20000 }).catch(() => console.warn(`   still loading on ${page.url()}`))
  await page.addStyleTag({ content: HIDE_DEV_CHROME }).catch(() => {})
  await page.waitForTimeout(800)
}

async function capture(page, name, { fullPage = false } = {}) {
  await settle(page)
  const file = path.join(out, `${name}.png`)
  mkdirSync(path.dirname(file), { recursive: true })
  await page.screenshot({ path: file, fullPage })
  console.log(`   ${file}`)
}

const browser = await playwright.chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  colorScheme: "light",
})
await context.addInitScript(() => {
  try { if (!localStorage.getItem("fabric-theme")) localStorage.setItem("fabric-theme", "light") } catch {}
})
const page = await context.newPage()
page.setDefaultTimeout(60000)

const want = (name) => only.size === 0 || only.has(name)

// Bring the deployment tabs to the top of the scrolling page, so a Logs shot shows the
// stage timeline and the log rather than the header above them.
async function showTabs(current) {
  await current.getByRole("tablist").first().evaluate((element) => element.scrollIntoView({ block: "start" }))
  await current.waitForTimeout(300)
}

async function visit(name, route, action, options) {
  if (!want(name)) return
  await page.goto(`${base}${route}`, { waitUntil: "domcontentloaded" })
  await settle(page)
  if (action) await action(page)
  await capture(page, name, options)
}

// --- signed out -------------------------------------------------------------------------
await visit("01-landing", "/")

// --- sign in through the mock identity provider -----------------------------------------
await page.goto(`${base}/auth/login?returnTo=/onboarding`, { waitUntil: "domcontentloaded" })
await page.waitForURL((url) => url.pathname.startsWith("/onboarding"), { timeout: 120000 })
await settle(page)
if (want("02-onboarding-account-switcher")) await capture(page, "02-onboarding-account-switcher")
await page.locator(`form:has(input[name="accountId"][value="${state.account_id}"]) button[type="submit"]`).click()
await page.waitForURL((url) => url.pathname.startsWith("/dashboard"), { timeout: 120000 })

// The sidebar's account menu, naming both accounts from /v1/me.
await visit("02b-account-switcher-menu", "/dashboard", async (current) => {
  await current.getByRole("button", { name: "Open account menu" }).click()
  await current.getByRole("menu").waitFor()
})

// --- console pages ----------------------------------------------------------------------
const deploymentId = state.deployment_ids["support-assistant"]
await visit("03-dashboard", "/dashboard")
await visit("05-deployments", "/deployments")
await visit("06-deployment-detail", `/deployments/${deploymentId}`)
// The Runtime and Routing cards: kernel mode, balancing strategy, capabilities, priority
// and cascade tier.
await visit("06b-deployment-detail-routing", `/deployments/${deploymentId}`, async (current) => {
  await current.getByText("Balancing strategy", { exact: true }).scrollIntoViewIfNeeded()
  await current.evaluate(() => document.querySelector(".fabric-page-content")?.scrollBy(0, 160))
})
// Logs: where creating a deployment lands. The first deployment on a fresh machine pulling
// the model runtime image, one downloading its weights with measured progress, a ready one
// searched, and one that failed with a diagnosis and hint.
await visit("06c-deployment-logs-first-pull", `/deployments/${state.deployment_ids["docs-qa"]}?tab=logs`, async (current) => {
  await current.getByText("First deployment on this machine").first().waitFor()
  await showTabs(current)
})
await visit("06d-deployment-logs-downloading", `/deployments/${state.deployment_ids["code-review-bot"]}?tab=logs`, async (current) => {
  await current.getByLabel("Weight download progress").first().waitFor()
  await showTabs(current)
})
await visit("06e-deployment-logs-search", `/deployments/${deploymentId}?tab=logs`, async (current) => {
  await current.getByLabel("Search the log").first().fill("throughput")
  await current.waitForTimeout(300)
  await showTabs(current)
})
await visit("07b-deployment-logs-failed", `/deployments/${state.deployment_ids["long-context-eval"]}?tab=logs`, async (current) => {
  await current.getByText("Out of GPU memory").first().waitFor()
  await showTabs(current)
})
if (state.removed_stamp_deployment_id) {
  await visit("07c-deployment-stamp-removed", `/deployments/${state.removed_stamp_deployment_id}?tab=logs`, async (current) => {
    await current.getByText("Stamp removed").first().waitFor()
  })
}
// Metrics: the serving charts pushed by the stamp agent (docs/serving-metrics.md), for the
// last hour and then the last day, which shows the backfilled working-day curve.
// The page scrolls inside the shell rather than the document, so a taller window stands in
// for a full-page capture, scrolled to put the charts in view.
async function tall(run) {
  await page.setViewportSize({ width: 1440, height: 1900 })
  try { await run() } finally { await page.setViewportSize({ width: 1440, height: 900 }) }
}
async function openMetrics(current, window) {
  if (window) await current.getByRole("button", { name: `Last ${window}` }).click()
  await current.locator("[data-metric-chart] .recharts-line, [data-metric-chart] .recharts-area").first().waitFor()
  await current.locator("[role='tablist']").evaluate((element) => element.scrollIntoView({ block: "start" }))
  await current.waitForTimeout(800)
}
if (want("06f-deployment-metrics")) await tall(() => visit("06f-deployment-metrics", `/deployments/${deploymentId}?tab=metrics`, (current) => openMetrics(current)))
if (want("06g-deployment-metrics-24h")) await tall(() => visit("06g-deployment-metrics-24h", `/deployments/${deploymentId}?tab=metrics`, (current) => openMetrics(current, "24 hours")))
if (want("09f-stamp-metrics")) await tall(() => visit("09f-stamp-metrics", `/stamps/${state.stamps["aks-gpu"]}`, async (current) => {
  await current.locator("[data-gpu]").first().waitFor()
  await current.waitForTimeout(800)
}))
await visit("07-deployment-detail-failed-status", `/deployments/${state.deployment_ids["long-context-eval"]}`, async (current) => {
  await current.getByRole("tab", { name: "Status" }).click()
})
await visit("08-deployment-new", "/deployments/new")
await visit("08a-deployment-new-confirm", "/deployments/new?model=qwen3.5-9b")
// The create drawer: the model picker, a catalog model's confirmation step, then the
// custom form with its Routing section filled in for an agent workload.
async function openCreateDrawer(current) {
  await current.getByRole("button", { name: "Create deployment" }).first().click()
  await current.getByRole("dialog").waitFor()
}
await visit("drawers/create-deployment", "/deployments", openCreateDrawer)
await visit("drawers/create-deployment-confirm", "/deployments", async (current) => {
  await openCreateDrawer(current)
  await current.getByRole("dialog").getByRole("button", { name: /Qwen3\.5 9B/ }).click()
  await current.getByRole("textbox", { name: "Deployment name", exact: true }).waitFor()
})
await visit("08b-deployment-drawer-routing", "/deployments", async (current) => {
  await openCreateDrawer(current)
  const dialog = current.getByRole("dialog")
  await dialog.getByRole("button", { name: /^Custom model/ }).click()
  await dialog.getByRole("textbox", { name: "Model repository", exact: true }).fill("Qwen/Qwen3.5-4B")
  await dialog.getByRole("textbox", { name: "Deployment name", exact: true }).fill("agent-planner")
  await dialog.getByRole("textbox", { name: "API model name", exact: true }).fill("qwen3.5-4b-agent")
  await dialog.getByText("Routing", { exact: true }).click()
  await dialog.getByRole("combobox", { name: "Balancing strategy" }).click()
  await current.getByRole("option", { name: "Prefix aware" }).click()
  await dialog.getByRole("checkbox", { name: /^Code/ }).click()
  await dialog.getByRole("checkbox", { name: /^Reasoning/ }).click()
  await dialog.getByRole("spinbutton", { name: "Priority", exact: true }).fill("20")
  await dialog.getByRole("spinbutton", { name: "Cascade tier", exact: true }).fill("1")
  await dialog.locator("details[data-section='routing']").evaluate((element) => element.scrollIntoView({ block: "end" }))
})
await visit("09-stamps", "/stamps")
await visit("09b-stamps-show-removed", "/stamps?removed=1")
// The registration and activity log right after the one-command bootstrap.
await visit("09c-stamp-activity", `/stamps/${state.stamps["rtx-pro-6000"]}`, async (current) => {
  await current.getByTestId("activity-headline").waitFor()
})
await visit("09d-stamp-activity-warnings", `/stamps/${state.stamps["vm-gpu"]}`, async (current) => {
  await current.getByTestId("activity-headline").waitFor()
})
await visit("09e-stamp-remove-dialog", "/stamps", async (current) => {
  await current.getByRole("button", { name: "Remove", exact: true }).first().click()
  await current.getByRole("dialog").waitFor()
})
await visit("10-stamps-enroll-dialog", "/stamps", async (current) => {
  await current.getByRole("button", { name: /enroll|add stamp|connect/i }).first().click()
  await current.getByRole("dialog").waitFor()
})
await visit("11-usage", "/usage")
await visit("12-audit", "/audit")
await visit("13-audit-filtered", "/audit?action=deployment.created")
await visit("14-access-api-keys", "/access/api-keys")
await visit("15-access-members", "/access/members")
await visit("16-access-service-principals", "/access/service-principals")
await visit("17-access-identity-provider", "/access/identity-provider")
await visit("17b-identity-provider-drawer", "/access/identity-provider", async (current) => {
  await current.getByRole("button", { name: "Edit provider" }).click()
  await current.getByRole("dialog").waitFor()
})
await visit("18-settings", "/settings")
// The playground compares two models on different stamps (aks-gpu and gcp-rtx6000). The
// demo console's gateway is the edge, which routes each to the stamp serving it.
async function chooseModel(current, slot, label) {
  await current.getByRole("button", { name: `Choose model ${slot}` }).click()
  await current.getByPlaceholder("Search your deployments…").fill(label)
  await current.getByRole("option", { name: new RegExp(label.replace(/[.]/g, "\\.")) }).first().click()
}
await visit("19-playground", "/playground", async (current) => {
  await chooseModel(current, "A", "qwen3.5-2b · support-assistant")
  await chooseModel(current, "B", "qwen3.5-2b-large · assistant-large")
  if (process.env.DEMO_DEBUG) await current.screenshot({ path: path.join(out, "debug-playground.png") })
  const prompt = current.getByLabel("Prompt for both models")
  await prompt.fill("In a few sentences: how does Fabric decide which stamp serves a deployment?")
  await prompt.press("Enter")
  // Wait until both streamed answers have finished.
  await current.getByText("Complete", { exact: true }).nth(1).waitFor({ timeout: 90000 })
  await current.waitForTimeout(2000)
})
// At 1440 px every table fits its card. In a narrower window the table scrolls sideways
// inside the card, with an edge fade on the side that has more columns.
if (want("05b-deployments-narrow")) {
  await page.setViewportSize({ width: 1100, height: 900 })
  await visit("05b-deployments-narrow", "/deployments")
  await page.setViewportSize({ width: 1440, height: 900 })
}
await visit("20-architecture", "/architecture")

// --- agents -------------------------------------------------------------------------------
// Runs seeded on aks-gpu, listed through the edge gateway.
await visit("21-agents-runs", "/agents/runs")
await visit("22-agents-run-detail", "/agents/runs/trip-planner-7f2c")
await visit("23-agents-playground", "/agents/playground")
// The Ctrl+K palette: actions first, then every console page.
await visit("24-command-palette", "/dashboard", async (current) => {
  await current.keyboard.press("Control+k")
  await current.getByRole("dialog").waitFor()
  await current.waitForTimeout(300)
})

// --- routing policies -----------------------------------------------------------------------
// The list, the visual editor with a rule selected, the test panel answering a described
// request, and the publish dialog's draft-vs-live diff (docs/routing-policies.md).
const routingPolicy = state.routing_policies?.["chat-default"]
async function openTestPanel(current) {
  await current.getByRole("tab", { name: /Test a request/ }).click()
  await current.getByLabel("Session key").fill("support-thread-4821")
  await current.getByLabel("Prompt tokens", { exact: true }).fill("2400")
  await current.getByRole("button", { name: "Run test" }).click()
  await current.getByText("Traffic split").waitFor()
  await current.getByRole("tab", { name: /Test a request/ }).evaluate((element) => element.scrollIntoView({ block: "start" }))
  await current.waitForTimeout(600)
}
async function waitForFlow(current) {
  await current.locator(".react-flow__node-rule").first().waitFor()
  await current.waitForTimeout(600)
}
async function openPublish(current) {
  await waitForFlow(current)
  await current.getByRole("button", { name: "Publish" }).click()
  await current.getByRole("dialog").waitFor()
}
if (routingPolicy) {
  await visit("routing/25-routing", "/routing")
  if (want("routing/26-routing-editor")) await tall(() => visit("routing/26-routing-editor", `/routing/${routingPolicy}`, waitForFlow))
  if (want("routing/27-routing-test")) await tall(() => visit("routing/27-routing-test", `/routing/${routingPolicy}`, openTestPanel))
  await visit("routing/28-routing-publish-diff", `/routing/${routingPolicy}`, openPublish)
  if (want("routing/29-routing-editor-mobile")) {
    await page.setViewportSize({ width: 390, height: 1600 })
    await visit("routing/29-routing-editor-mobile", `/routing/${routingPolicy}`)
    await page.setViewportSize({ width: 1440, height: 900 })
  }
}

// --- dark theme -------------------------------------------------------------------------
// The home page, then the main pages and a drawer again in dark (dark/).
const darkShots = [
  ["04-dashboard-dark", "/dashboard"],
  ["dark/deployments", "/deployments"],
  ["dark/deployment-detail", `/deployments/${deploymentId}`],
  ["dark/stamps", "/stamps"],
  ["dark/usage", "/usage"],
  ["dark/agents-runs", "/agents/runs"],
  ["dark/settings", "/settings"],
  ["dark/create-deployment", "/deployments", openCreateDrawer],
  ["dark/command-palette", "/dashboard", async (current) => {
    await current.keyboard.press("Control+k")
    await current.getByRole("dialog").waitFor()
    await current.waitForTimeout(300)
  }],
  ["dark/onboarding", "/onboarding?switch=true"],
  ...(routingPolicy ? [
    ["dark/routing", "/routing"],
    ["dark/routing-editor", `/routing/${routingPolicy}`, waitForFlow],
    ["dark/routing-test", `/routing/${routingPolicy}`, openTestPanel],
  ] : []),
]
if (darkShots.some(([name]) => want(name))) {
  await page.evaluate(() => localStorage.setItem("fabric-theme", "dark"))
  await page.emulateMedia({ colorScheme: "dark" })
  for (const [name, route, action] of darkShots) await visit(name, route, action)
  await page.evaluate(() => localStorage.setItem("fabric-theme", "light"))
  await page.emulateMedia({ colorScheme: "light" })
}

await browser.close()
