import { strict as assert } from "node:assert"
import { test } from "node:test"

import { closeDecision, FORM_DRAWER_WIDTHS, opensInPlace, showsSuccess } from "../lib/form-drawer"

test("a pending submission cannot be closed, even when forced", () => {
  assert.equal(closeDecision({ pending: true, dirty: false }), "block")
  assert.equal(closeDecision({ pending: true, dirty: true }), "block")
  assert.equal(closeDecision({ pending: true, dirty: true, force: true }), "block")
  assert.equal(closeDecision({ pending: true, dirty: false, succeeded: true }), "block")
})

test("edited input asks before closing; untouched, succeeded and forced closes do not", () => {
  assert.equal(closeDecision({ pending: false, dirty: true }), "confirm")
  assert.equal(closeDecision({ pending: false, dirty: false }), "close")
  assert.equal(closeDecision({ pending: false, dirty: true, succeeded: true }), "close")
  assert.equal(closeDecision({ pending: false, dirty: true, force: true }), "close")
})

test("the success view needs a successful, error-free result and a view to show", () => {
  assert.equal(showsSuccess({ ok: true }, true), true)
  assert.equal(showsSuccess({ ok: true }, false), false)
  assert.equal(showsSuccess({}, true), false)
  assert.equal(showsSuccess({ error: "Denied" }, true), false)
  assert.equal(showsSuccess({ ok: true, error: "Partial" }, true), false)
})

test("a link trigger opens in place only for a plain primary click", () => {
  const plain = { button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, defaultPrevented: false }
  assert.equal(opensInPlace(plain), true)
  for (const modified of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }, { defaultPrevented: true }]) {
    assert.equal(opensInPlace({ ...plain, ...modified }), false)
  }
})

test("drawer widths are complete class names scoped to the right-side sheet", () => {
  for (const value of Object.values(FORM_DRAWER_WIDTHS)) assert.match(value, /^data-\[side=right\]:sm:max-w-\[\d+px\]$/)
  const pixels = (value: string) => Number(/\[(\d+)px\]/.exec(value)![1])
  assert.ok(pixels(FORM_DRAWER_WIDTHS.default) >= 480 && pixels(FORM_DRAWER_WIDTHS.default) <= 560)
  assert.ok(pixels(FORM_DRAWER_WIDTHS.wide) > pixels(FORM_DRAWER_WIDTHS.default))
  assert.ok(pixels(FORM_DRAWER_WIDTHS.xl) > pixels(FORM_DRAWER_WIDTHS.wide))
})
