import { strict as assert } from "node:assert"
import { test } from "node:test"

import { createLiveRefresh } from "../lib/fabric/live-refresh"

test("polling has one request in flight, pauses hidden tabs, resumes and stops cleanly", async () => {
  let visible = true
  let calls = 0
  let release: () => void = () => {}
  let callback: (() => void) | undefined
  let canceled = 0
  const controller = createLiveRefresh({
    refresh: async () => { calls++; await new Promise<void>((resolve) => { release = resolve }) },
    isVisible: () => visible,
    schedule: (next, delay) => { assert.equal(delay, 8000); callback = next; return 1 as unknown as ReturnType<typeof setTimeout> },
    cancel: () => { canceled++; callback = undefined },
  })
  controller.start()
  assert.ok(callback)
  const first = controller.run()
  await Promise.resolve()
  assert.equal(calls, 1)
  assert.equal(controller.run(), first)
  assert.equal(controller.run(true), first)
  release()
  await first
  visible = false
  await controller.run()
  assert.equal(calls, 1)
  assert.ok(callback)
  visible = true
  const next = controller.run()
  await Promise.resolve()
  assert.equal(calls, 2)
  controller.stop()
  release()
  await next
  assert.equal(callback, undefined)
  assert.ok(canceled > 0)
  await controller.run(true)
  assert.equal(calls, 2)
})

test("manual refresh works while hidden and a rejected refresh remains recoverable", async () => {
  let calls = 0
  let schedules = 0
  const controller = createLiveRefresh({
    refresh: async () => { if (++calls === 1) throw new Error("offline") },
    isVisible: () => false,
    schedule: () => { schedules++; return 1 as unknown as ReturnType<typeof setTimeout> },
    cancel: () => {}, intervalMs: 1,
  })
  await assert.rejects(controller.run(true), /offline/)
  await controller.run(true)
  assert.equal(calls, 2)
  assert.equal(schedules, 2)
  controller.stop()
})
