// Pure decisions behind the console's form drawers (components/console/form-drawer.tsx),
// kept free of React so they can be tested with plain `node`.

export type FormDrawerSize = "default" | "wide" | "xl"

/**
 * Desktop widths of the right-side sheet (full class names, so Tailwind finds them); below
 * the mobile breakpoint a bottom drawer is used instead.
 */
export const FORM_DRAWER_WIDTHS: Record<FormDrawerSize, string> = {
  default: "data-[side=right]:sm:max-w-[520px]",
  wide: "data-[side=right]:sm:max-w-[680px]",
  xl: "data-[side=right]:sm:max-w-[880px]",
}

export type CloseDecision = "block" | "confirm" | "close"

/**
 * What a request to close the drawer (Escape, backdrop, close button, Cancel, swipe) does.
 * A pending submission cannot be abandoned; edited input needs confirmation unless the
 * form has already succeeded (nothing is left to lose) or the caller forces the close.
 */
export function closeDecision({ pending, dirty, succeeded = false, force = false }: {
  pending: boolean
  dirty: boolean
  succeeded?: boolean
  force?: boolean
}): CloseDecision {
  if (pending) return "block"
  if (force || succeeded || !dirty) return "close"
  return "confirm"
}

/** The state shape returned by the console's server actions. */
export type FormActionState = { ok?: boolean; error?: string }

/** A drawer shows its success view (and a Done button) once its action reports success. */
export function showsSuccess(state: FormActionState, hasSuccessView: boolean) {
  return hasSuccessView && state.ok === true && !state.error
}

type ClickLike = {
  button: number
  metaKey: boolean
  ctrlKey: boolean
  shiftKey: boolean
  altKey: boolean
  defaultPrevented: boolean
}

/**
 * A trigger rendered as a link (e.g. to /deployments/new) opens the drawer in place for a
 * plain primary click, and keeps the link's own behaviour for modified clicks (new tab,
 * new window, download) and when JavaScript is unavailable.
 */
export function opensInPlace(event: ClickLike) {
  return !event.defaultPrevented && event.button === 0
    && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey
}
