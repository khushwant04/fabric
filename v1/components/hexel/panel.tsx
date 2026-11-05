import * as React from "react"
import { cn } from "cn"

/**
 * A tray with a raised card in it: the tray carries the section's header
 * (a title, a picker, a badge) and the card holds the content, sitting flush
 * with the tray's sides and bottom so the tray shows as a band across the top
 * and a soft U behind the card's corners. A console component: use it to
 * hold a table under its title and actions, a settings section, API keys,
 * sessions and devices. A table inside a panel stays borderless; the panel is
 * the only frame.
 *
 * Panel > PanelHeader (PanelTitle, PanelAction) > PanelContent (PanelRow,
 * PanelField) > PanelFooter. A footer after the content sits in the tray
 * under the card.
 */
function Panel({ className, ...props }: React.ComponentProps<"section">) {
  return (
    <section
      data-slot="panel"
      className={cn(
        "flex flex-col rounded-[var(--radius-surface)] bg-[color-mix(in_oklab,var(--muted)_65%,var(--background))] shadow-[inset_0_0_0_1px_var(--border)] dark:bg-[color-mix(in_oklab,var(--background)_78%,black)]",
        className
      )}
      {...props}
    />
  )
}

function PanelHeader({ className, ...props }: React.ComponentProps<"header">) {
  return (
    <header
      data-slot="panel-header"
      className={cn(
        "flex min-h-11 items-center gap-2 px-4 py-2 text-sm [&_svg:not([class*='size-'])]:size-4 [&_svg:not([class*='text-'])]:text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

function PanelTitle({ className, ...props }: React.ComponentProps<"h3">) {
  return (
    <h3
      data-slot="panel-title"
      className={cn("flex items-center gap-2 font-medium", className)}
      {...props}
    />
  )
}

/** Sits at the right of the header: a badge, a link, a small button. */
function PanelAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="panel-action"
      className={cn("ml-auto flex shrink-0 items-center gap-2", className)}
      {...props}
    />
  )
}

/** The raised card. Flush with the tray's edges; its ring lies on the tray's. */
function PanelContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="panel-content"
      className={cn(
        "relative -mx-px flex flex-col rounded-[var(--radius-surface)] bg-card shadow-[0_0_0_1px_var(--border),0_1px_2px_rgb(0_0_0/0.04)] last:-mb-px",
        className
      )}
      {...props}
    />
  )
}

/** One line of content. Rows are split by inset hairlines that stop short of the card's edges. */
function PanelRow({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="panel-row"
      className={cn(
        "relative flex min-h-12 items-center gap-3 px-4 py-3 text-sm [&_svg:not([class*='size-'])]:size-4 [&+&]:before:absolute [&+&]:before:inset-x-4 [&+&]:before:top-0 [&+&]:before:h-px [&+&]:before:bg-border",
        className
      )}
      {...props}
    />
  )
}

/**
 * A labelled value inside a row: the label above in muted text, the value
 * below. `copy` adds a copy button for identifiers and keys.
 */
function PanelField({
  label,
  value,
  copy = false,
  mono = false,
  className,
}: {
  label: React.ReactNode
  value: string
  copy?: boolean
  mono?: boolean
  className?: string
}) {
  const [copied, setCopied] = React.useState(false)
  return (
    <div
      data-slot="panel-field"
      className={cn("flex min-w-0 flex-1 items-end gap-3", className)}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[13px] text-muted-foreground">{label}</span>
        <span className={cn("truncate", mono && "font-mono text-[13px]")}>
          {value}
        </span>
      </div>
      {copy && (
        <button
          type="button"
          aria-label={copied ? "Copied" : `Copy ${String(label)}`}
          onClick={() =>
            navigator.clipboard?.writeText(value).then(
              () => {
                setCopied(true)
                window.setTimeout(() => setCopied(false), 1200)
              },
              () => undefined
            )
          }
          className="grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors duration-(--motion-fast) outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <svg
            viewBox="0 0 16 16"
            aria-hidden="true"
            className="size-3.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            {copied ? (
              <path d="M3.5 8.5 6.5 11.5 12.5 4.5" />
            ) : (
              <>
                <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
                <path d="M10.5 3.5v-.5a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h.5" />
              </>
            )}
          </svg>
        </button>
      )}
    </div>
  )
}

/** Under the card, inside the tray: a hint, a link, a timestamp. */
function PanelFooter({ className, ...props }: React.ComponentProps<"footer">) {
  return (
    <footer
      data-slot="panel-footer"
      className={cn(
        "flex items-center gap-2 px-4 py-2.5 text-xs text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

export {
  Panel,
  PanelHeader,
  PanelTitle,
  PanelAction,
  PanelContent,
  PanelRow,
  PanelField,
  PanelFooter,
}
