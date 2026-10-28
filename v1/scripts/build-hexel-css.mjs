// Builds app/hexel.css from design-system/tokens.json, the Hexel Studio design system's
// token source. The console imports the result right after Tailwind and shadcn, so every
// stock shadcn component reads the system's values through shadcn's own variable names.
//
//   node scripts/build-hexel-css.mjs          write app/hexel.css
//   node scripts/build-hexel-css.mjs --check  exit 1 if app/hexel.css is out of date
//
// Change a value in the design system and refresh tokens.json; never edit hexel.css.

import { readFileSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const root = join(dirname(fileURLToPath(import.meta.url)), "..")
const tokensPath = join(root, "design-system", "tokens.json")
const outputPath = join(root, "app", "hexel.css")

// Motion lives beside tokens.json in the design system (motion.json); its values are
// carried here as the system documents them.
const MOTION = {
  "motion-fast": "100ms",
  "motion-base": "180ms",
  "motion-slow": "260ms",
  "motion-story": "520ms",
  "motion-field": "900ms",
}
const EASING = {
  strong: "cubic-bezier(0.16, 1, 0.3, 1)",
  "in-strong": "cubic-bezier(0.7, 0, 0.84, 0)",
  emphasis: "cubic-bezier(0.34, 1.56, 0.64, 1)",
}
// shadcn's radius scale, as multiples of the system's base radius.
const RADIUS_SCALE = { xs: 0.4, sm: 0.6, md: 0.8, lg: 1, xl: 1.4, "2xl": 1.8, "3xl": 2.2, "4xl": 2.6 }
// Tailwind's shadow names fold onto the system's three elevations.
const SHADOW_ALIASES = {
  "2xs": "surface-shadow-small", xs: "surface-shadow-small", sm: "surface-shadow-small",
  md: "surface-shadow-menu", lg: "surface-shadow-menu", menu: "surface-shadow-menu",
  xl: "surface-shadow-dialog", "2xl": "surface-shadow-dialog", dialog: "surface-shadow-dialog",
  ring: "surface-shadow-ring", composer: "surface-shadow-composer", glow: "surface-shadow-glow",
}
// The product type scale and mono styles become utilities; the site's scale does not
// belong in the console.
const TYPE_GROUPS = new Set(["Product UI", "Mono"])

function resolve(value) {
  // "{foreground}" refers to another token.
  return typeof value === "string" ? value.replace(/\{([a-z0-9-]+)\}/g, "var(--$1)") : value
}

function themeValues(value) {
  if (typeof value === "string") return { light: resolve(value) }
  return Object.fromEntries(Object.entries(value).map(([theme, entry]) => [theme, resolve(entry)]))
}

function block(selector, declarations) {
  if (!declarations.length) return ""
  return `${selector} {\n${declarations.map((line) => `  ${line}`).join("\n")}\n}\n`
}

export function build(tokens) {
  const light = []
  const dark = []
  const contrast = []
  const theme = []

  for (const token of tokens.color.tokens) {
    const values = themeValues(token.value)
    light.push(`--${token.name}: ${values.light};`)
    if (values.dark !== undefined && values.dark !== values.light) dark.push(`--${token.name}: ${values.dark};`)
    if (values["high-contrast"] !== undefined) contrast.push(`--${token.name}: ${values["high-contrast"]};`)
    theme.push(`--color-${token.name}: var(--${token.name});`)
  }

  for (const token of tokens.shadow.tokens) {
    const values = themeValues(token.value)
    light.push(`--${token.name}: ${values.light};`)
    if (values.dark !== undefined && values.dark !== values.light) dark.push(`--${token.name}: ${values.dark};`)
  }
  for (const [alias, token] of Object.entries(SHADOW_ALIASES)) theme.push(`--shadow-${alias}: var(--${token});`)

  const radii = Object.fromEntries(tokens.radius.tokens.map((token) => [token.name, token.value]))
  light.push(`--radius: ${radii.radius};`)
  for (const [step, factor] of Object.entries(RADIUS_SCALE)) {
    theme.push(`--radius-${step}: ${factor === 1 ? "var(--radius)" : `calc(var(--radius) * ${factor})`};`)
  }
  const namedRadii = []
  for (const [name, value] of Object.entries(radii)) {
    if (name === "radius") continue
    namedRadii.push(`--${name}: ${value};`)
  }

  for (const [name, value] of Object.entries(MOTION)) light.push(`--${name}: ${value};`)
  for (const [name, value] of Object.entries(EASING)) theme.push(`--ease-${name}: ${value};`)

  const families = tokens.type.families
  theme.push(`--font-sans: var(--font-geist-sans), ${families.sans};`)
  theme.push(`--font-mono: var(--font-geist-mono), ${families.mono};`)
  theme.push(`--font-heading: var(--font-sans);`)

  const utilities = []
  for (const group of tokens.type.groups) {
    if (!TYPE_GROUPS.has(group.name)) continue
    for (const style of group.styles ?? group.tokens ?? []) {
      const lines = [
        `font-size: ${style.fontSize};`,
        `line-height: ${style.lineHeight};`,
        `font-weight: ${style.fontWeight};`,
      ]
      if (style.letterSpacing) lines.push(`letter-spacing: ${style.letterSpacing};`)
      if (style.name === "ui-eyebrow") {
        if (!style.letterSpacing) lines.push("letter-spacing: 0.08em;")
        lines.push("text-transform: uppercase;")
      }
      // The font itself, not var(--font-mono): `@theme inline` variables are inlined into
      // Tailwind's utilities and never emitted, so a var() here would resolve to nothing.
      if (group.name === "Mono") lines.push(`font-family: var(--font-geist-mono), ${tokens.type.families.mono};`)
      utilities.push(`@utility ${style.name} {\n${lines.map((line) => `  ${line}`).join("\n")}\n}\n`)
    }
  }

  const header = [
    "/*",
    ` * ${tokens.name ?? "Hexel Studio"} design system tokens${tokens.version ? ` v${tokens.version}` : ""}, synced ${tokens.meta?.synced ?? "unknown"}.`,
    " * Generated by scripts/build-hexel-css.mjs from design-system/tokens.json. Do not edit:",
    " * change the design system, refresh tokens.json, and run `npm run tokens`.",
    " */",
    "",
  ].join("\n")

  return [
    header,
    block("@theme", namedRadii),
    block("@theme inline", theme),
    block(":root", light),
    block(".dark", ["color-scheme: dark;", ...dark]),
    block(".high-contrast", contrast),
    "@media (prefers-reduced-motion: reduce) {\n  :root {\n" +
      Object.keys(MOTION).map((name) => `    --${name}: 0ms;`).join("\n") +
      "\n  }\n}\n",
    ...utilities,
  ].filter(Boolean).join("\n")
}

const css = build(JSON.parse(readFileSync(tokensPath, "utf8")))
if (process.argv.includes("--check")) {
  let current = ""
  try { current = readFileSync(outputPath, "utf8") } catch { /* missing counts as stale */ }
  if (current !== css) {
    console.error("app/hexel.css is out of date with design-system/tokens.json; run `npm run tokens`.")
    process.exit(1)
  }
} else {
  writeFileSync(outputPath, css)
  console.log(`wrote ${outputPath}`)
}
