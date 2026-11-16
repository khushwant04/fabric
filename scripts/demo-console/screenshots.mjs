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

// The header's account switcher, naming both accounts from /v1/me.
await visit("02b-account-switcher-menu", "/dashboard", async (current) => {
  await current.getByRole("button", { name: "Switch account" }).click()
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
await visit("07-deployment-detail-failed-status", `/deployments/${state.deployment_ids["long-context-eval"]}`, async (current) => {
  await current.getByRole("tab", { name: "Status" }).click()
})
await visit("08-deployment-new", "/deployments/new")
// The create drawer, then its Routing section filled in for an agent workload.
async function openCreateDrawer(current) {
  await current.getByRole("button", { name: "Create deployment" }).first().click()
  await current.getByRole("dialog").waitFor()
}
await visit("drawers/create-deployment", "/deployments", openCreateDrawer)
await visit("08b-deployment-drawer-routing", "/deployments", async (current) => {
  await openCreateDrawer(current)
  const dialog = current.getByRole("dialog")
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

// --- dark theme -------------------------------------------------------------------------
if (want("04-dashboard-dark")) {
  await page.evaluate(() => localStorage.setItem("fabric-theme", "dark"))
  await page.emulateMedia({ colorScheme: "dark" })
  await page.goto(`${base}/dashboard`, { waitUntil: "domcontentloaded" })
  await capture(page, "04-dashboard-dark")
  await page.evaluate(() => localStorage.setItem("fabric-theme", "light"))
  await page.emulateMedia({ colorScheme: "light" })
}

await browser.close()
