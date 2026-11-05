// Builds app/hexel.css from design-system/, a copy of the Hexel Studio ui-registry's
// tokens/ (tokens.json, motion.json, gradients.json). It follows the registry's own
// scripts/build-theme.mjs, so the console resolves every variable, utility and keyframe
// the registry's components expect, under shadcn's variable names.
//
//   node scripts/build-hexel-css.mjs          write app/hexel.css
//   node scripts/build-hexel-css.mjs --check  exit 1 if app/hexel.css is out of date
//
// To take a new design-system release, copy the registry's tokens/ over design-system/
// and run this; never edit hexel.css.

import { readFileSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const root = join(dirname(fileURLToPath(import.meta.url)), "..")
const read = (name) => JSON.parse(readFileSync(join(root, "design-system", name), "utf8"))
const tokens = read("tokens.json")
const motion = read("motion.json")
const gradients = read("gradients.json")
const outputPath = join(root, "app", "hexel.css")

const ALIAS = /^\{(.+)\}$/
const value = (v) => (ALIAS.test(v) ? `var(--${v.match(ALIAS)[1]})` : v)
const perTheme = (token) => (typeof token.value === "string" ? { light: token.value } : token.value)
const isAlias = (v) => typeof v === "string" && ALIAS.test(v)

const colors = tokens.color.tokens
const shadows = tokens.shadow.tokens
const lengths = ["spacing", "size", "radius"].flatMap((family) => tokens[family].tokens)
const FLUID = { "section-y": "clamp(64px, 8vw, 112px)" }

function modeVars(mode) {
  const vars = {}
  for (const token of [...colors, ...shadows]) {
    const v = perTheme(token)
    if (mode === "light") vars[token.name] = value(v.light ?? Object.values(v)[0])
    else if (v.dark !== undefined) vars[token.name] = value(v.dark)
    // Aliases are re-declared so they resolve inside a nested .dark subtree.
    else if (isAlias(v.light)) vars[token.name] = value(v.light)
  }
  if (mode === "light") {
    for (const token of lengths) vars[token.name] = FLUID[token.name] ?? token.value
    for (const token of motion.tokens) vars[token.name] = token.value
    for (const token of gradients.tokens) if (token.name !== "gradient-scene") vars[token.name] = token.value
  }
  return vars
}

// The Tailwind bridge. next/font exposes Geist as --font-geist-*, which is what the
// registry's preset expects too.
const bridge = {
  "font-sans": "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif",
  "font-mono": "var(--font-geist-mono), ui-monospace, monospace",
  "font-heading": "var(--font-sans)",
  ...Object.fromEntries(colors.map((t) => [`color-${t.name}`, `var(--${t.name})`])),
  "shadow-2xs": "var(--surface-shadow-small)",
  "shadow-xs": "var(--surface-shadow-small)",
  "shadow-sm": "var(--surface-shadow-small)",
  "shadow-md": "var(--surface-shadow-menu)",
  "shadow-lg": "var(--surface-shadow-menu)",
  "shadow-xl": "var(--surface-shadow-dialog)",
  "shadow-2xl": "var(--surface-shadow-dialog)",
  "shadow-ring": "var(--surface-shadow-ring)",
  "shadow-menu": "var(--surface-shadow-menu)",
  "shadow-dialog": "var(--surface-shadow-dialog)",
  "shadow-glow": "var(--surface-shadow-glow)",
  "radius-xs": "calc(var(--radius) * 0.4)",
  "radius-sm": "calc(var(--radius) * 0.6)",
  "radius-md": "calc(var(--radius) * 0.8)",
  "radius-lg": "var(--radius)",
  "radius-xl": "calc(var(--radius) * 1.4)",
  "radius-2xl": "calc(var(--radius) * 1.8)",
  "radius-3xl": "calc(var(--radius) * 2.2)",
  "radius-4xl": "calc(var(--radius) * 2.6)",
  ...Object.fromEntries(motion.tokens.filter((t) => t.name.startsWith("ease-")).map((t) => [t.name, t.value])),
  "animate-shimmer": "hx-shimmer 1.6s linear infinite",
  "animate-rise": "hx-rise var(--motion-slow) var(--ease-strong) both",
  "animate-ping-soft": "hx-ping 2s var(--ease-strong) infinite",
  "animate-caret-blink": "hx-caret-blink 1.1s ease-out infinite",
  "animate-pop-in": "hx-pop-in var(--motion-fast) var(--ease-sharp) both",
  "animate-pop-out": "hx-pop-out var(--motion-fast) linear both",
}

// The registry emits the site's type groups as utilities. The console also gets the
// "Product UI" and "Mono" styles (ui-*, mono, code), which tokens.json defines for apps.
// A family the console loads through next/font is referenced by its variable, because the
// font's own name is hashed.
const TYPE_GROUPS = ["Display", "Text", "Navigation", "Brand", "Editorial", "Product UI", "Mono"]
const FAMILY = { mono: bridge["font-mono"] }
const typeUtilities = Object.fromEntries(
  tokens.type.groups
    .filter((group) => TYPE_GROUPS.includes(group.name))
    .flatMap((group) =>
      group.styles.map((style) => [
        style.name,
        {
          ...(group.family !== "sans" && { "font-family": FAMILY[group.family] ?? tokens.type.families[group.family] }),
          "font-size": style.fluid ?? style.fontSize,
          "line-height": String(style.lineHeight),
          "font-weight": String(style.fontWeight),
          ...(style.letterSpacing && { "letter-spacing": style.letterSpacing }),
          ...(style.color && { color: value(style.color) }),
          ...(style.wrap && { "text-wrap": style.wrap }),
          ...(style.numeric && { "font-variant-numeric": style.numeric }),
        },
      ])
    )
)

const base = {
  'button:not(:disabled), [role="button"]:not(:disabled)': { cursor: "pointer" },
  "::selection": { "background-color": "var(--selection)" },
}

const keyframes = {
  "hx-shimmer": { from: { "background-position": "150% 0" }, to: { "background-position": "-50% 0" } },
  "hx-rise": { from: { opacity: "0", transform: "translateY(8px)" }, to: { opacity: "1", transform: "none" } },
  "hx-ping": { "0%": { transform: "scale(1)", opacity: "0.45" }, "80%, 100%": { transform: "scale(2.4)", opacity: "0" } },
  "hx-caret-blink": { "0%, 70%, 100%": { opacity: "1" }, "20%, 50%": { opacity: "0" } },
  "hx-pop-in": {
    from: { opacity: "0", transform: "translate(var(--pop-x, 0px), var(--pop-y, -2px))" },
    to: { opacity: "1", transform: "none" },
  },
  "hx-pop-out": { to: { opacity: "0" } },
}
const reduced = Object.fromEntries(
  motion.tokens.filter((t) => t.name.startsWith("motion-")).map((t) => [`--${t.name}`, "0ms"])
)

const decls = (vars) => Object.entries(vars).map(([k, v]) => `  --${k}: ${v};`).join("\n")
function rule(selector, body, indent = "") {
  const lines = []
  for (const [k, v] of Object.entries(body)) {
    if (typeof v === "object") lines.push(rule(k, v, indent + "  "))
    else lines.push(`${indent}  ${k}: ${v};`)
  }
  return `${indent}${selector} {\n${lines.join("\n")}\n${indent}}`
}

const css = `/* Generated by scripts/build-hexel-css.mjs from design-system/. Do not edit: copy
   the ui-registry's tokens/ into design-system/ and run \`npm run tokens\`. */

@theme inline {
${decls(bridge)}
}

:root {
${decls(modeVars("light"))}
}

.dark {
${decls(modeVars("dark"))}
}

${rule("@layer base", base)}

${Object.entries(typeUtilities).map(([k, v]) => rule(`@utility ${k}`, v)).join("\n\n")}

${Object.entries(keyframes).map(([k, v]) => rule(`@keyframes ${k}`, v)).join("\n\n")}

${rule("@media (prefers-reduced-motion: reduce)", { ":root": reduced })}
`

if (process.argv.includes("--check")) {
  let current = ""
  try {
    current = readFileSync(outputPath, "utf8")
  } catch {}
  if (current !== css) {
    console.error("app/hexel.css is out of date with design-system/. Run `npm run tokens`.")
    process.exit(1)
  }
  console.log("app/hexel.css is up to date")
} else {
  writeFileSync(outputPath, css)
  console.log(`wrote app/hexel.css: ${colors.length} colours, ${shadows.length} shadows, ${Object.keys(typeUtilities).length} type styles`)
}
