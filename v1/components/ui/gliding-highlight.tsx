"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

const MOVEMENT = "160ms cubic-bezier(0.16, 1, 0.3, 1)"
const FADE = "opacity 100ms ease-out"
const TRANSITION = `transform ${MOVEMENT}, width ${MOVEMENT}, height ${MOVEMENT}, ${FADE}`

type GlidingHighlightOptions = {
  itemSelector: string
  selectedSelector?: string
  /** For menus that move keyboard focus with aria-activedescendant. */
  activeSelector?: string
  /** Keep one hover track across labels and padding in a navigation container. */
  hoverMode?: "item" | "nearest"
}

/** One background moves beneath a container's stationary interactive items. */
function useGlidingHighlight<T extends HTMLElement = HTMLDivElement>({
  itemSelector,
  selectedSelector,
  activeSelector,
  hoverMode = "item",
}: GlidingHighlightOptions) {
  const highlightRef = React.useRef<HTMLElement | null>(null)
  const cleanupRef = React.useRef<(() => void) | null>(null)
  const mountedContainerRef = React.useRef<T | null>(null)
  const attachedRef = React.useRef(false)

  const containerRef = React.useCallback(
    (container: T | null) => {
      if (!container) {
        attachedRef.current = false
        queueMicrotask(() => {
          if (attachedRef.current) return
          cleanupRef.current?.()
          cleanupRef.current = null
          mountedContainerRef.current = null
        })
        return
      }
      attachedRef.current = true
      if (mountedContainerRef.current === container) return
      cleanupRef.current?.()
      cleanupRef.current = null
      mountedContainerRef.current = container

      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)")
      let hovered: HTMLElement | null = null
      let focused: HTMLElement | null = null
      let lastInput: "pointer" | "keyboard" = "keyboard"
      let visible = false
      let geometry: { x: number; y: number; width: number; height: number } | null = null
      let frame = 0

      const usable = (item: HTMLElement | null): item is HTMLElement =>
        !!item &&
        container.contains(item) &&
        item.getClientRects().length > 0 &&
        !item.matches(':disabled, [aria-disabled="true"], [data-disabled]:not([data-disabled="false"])')

      const itemFrom = (target: EventTarget | null) => {
        const item = target instanceof Element ? target.closest<HTMLElement>(itemSelector) : null
        return usable(item) ? item : null
      }

      const nearestItem = (x: number, y: number) => {
        let nearest: HTMLElement | null = null
        let distance = Number.POSITIVE_INFINITY
        for (const item of container.querySelectorAll<HTMLElement>(itemSelector)) {
          if (!usable(item)) continue
          const bounds = item.getBoundingClientRect()
          const dx = Math.max(bounds.left - x, 0, x - bounds.right)
          const dy = Math.max(bounds.top - y, 0, y - bounds.bottom)
          const nextDistance = dx * dx + dy * dy
          if (nextDistance < distance) {
            nearest = item
            distance = nextDistance
          }
        }
        return nearest
      }

      const find = (selector?: string) => {
        if (!selector) return null
        for (const element of container.querySelectorAll<HTMLElement>(selector)) {
          const item = itemFrom(element)
          if (item) return item
        }
        return null
      }

      const paint = (item: HTMLElement | null, instant = false) => {
        const highlight = highlightRef.current
        if (!highlight) return
        if (reducedMotion.matches) {
          highlight.style.setProperty("transition-duration", "0s", "important")
        } else if (highlight.style.getPropertyPriority("transition-duration") === "important") {
          highlight.style.removeProperty("transition-duration")
        }
        if (!usable(item)) {
          highlight.style.transition = reducedMotion.matches ? "none" : FADE
          if (reducedMotion.matches) highlight.style.setProperty("transition-duration", "0s", "important")
          highlight.style.opacity = "0"
          visible = false
          return
        }

        const bounds = container.getBoundingClientRect()
        const itemBounds = item.getBoundingClientRect()
        const scaleX = bounds.width / container.offsetWidth || 1
        const scaleY = bounds.height / container.offsetHeight || 1
        const x = (itemBounds.left - bounds.left) / scaleX + container.scrollLeft - container.clientLeft
        const y = (itemBounds.top - bounds.top) / scaleY + container.scrollTop - container.clientTop
        const transform = `translate3d(${x}px, ${y}px, 0)`
        const width = `${itemBounds.width / scaleX}px`
        const height = `${itemBounds.height / scaleY}px`
        const nextGeometry = { x, y, width: itemBounds.width / scaleX, height: itemBounds.height / scaleY }
        if (visible && !reducedMotion.matches && geometry && Math.abs(geometry.x - x) < 0.05 && Math.abs(geometry.y - y) < 0.05 && Math.abs(geometry.width - nextGeometry.width) < 0.05 && Math.abs(geometry.height - nextGeometry.height) < 0.05) return
        geometry = nextGeometry
        const firstEntry = !visible

        highlight.style.transition = reducedMotion.matches
          ? "none"
          : firstEntry || instant
            ? FADE
            : TRANSITION
        highlight.style.transform = transform
        highlight.style.width = width
        highlight.style.height = height
        highlight.style.opacity = "1"
        if (reducedMotion.matches) highlight.style.setProperty("transition-duration", "0s", "important")
        visible = true
      }

      const current = () => {
        if (lastInput === "pointer" && usable(hovered)) return hovered
        if (lastInput === "keyboard") {
          const active = find(activeSelector)
          if (active) return active
          if (usable(focused)) return focused
        }
        return find(selectedSelector)
      }

      const refresh = (instant = false) => paint(current(), instant)
      const schedule = (instant = false) => {
        cancelAnimationFrame(frame)
        frame = requestAnimationFrame(() => refresh(instant))
      }

      const pointerOver = (event: PointerEvent) => {
        if (event.pointerType === "touch") return
        const item = itemFrom(event.target) ?? (
          hoverMode === "nearest" ? nearestItem(event.clientX, event.clientY) : null
        )
        if (!item) return // Crossing padding or separators keeps the last position.
        hovered = item
        lastInput = "pointer"
        paint(item)
      }
      const pointerMove = (event: PointerEvent) => {
        if (event.pointerType === "touch") return
        const item = itemFrom(event.target) ?? nearestItem(event.clientX, event.clientY)
        if (!item) return
        if (hovered === item && lastInput === "pointer") return
        hovered = item
        lastInput = "pointer"
        schedule()
      }
      const pointerLeave = () => {
        hovered = null
        if (lastInput === "keyboard") {
          paint(find(activeSelector) ?? (usable(focused) ? focused : find(selectedSelector)))
        } else {
          paint(find(selectedSelector))
        }
      }
      const focusIn = (event: FocusEvent) => {
        const item = itemFrom(event.target)
        if (!item) return
        focused = item
        // Clicking a row focuses it too; retain pointer behavior until keyboard focus.
        if (lastInput === "pointer" && !item.matches(":focus-visible")) return
        lastInput = "keyboard"
        paint(item)
      }
      const focusOut = (event: FocusEvent) => {
        if (event.relatedTarget instanceof Node && container.contains(event.relatedTarget)) return
        focused = null
        paint(lastInput === "pointer" && usable(hovered) ? hovered : find(selectedSelector))
      }
      const keyDown = () => {
        lastInput = "keyboard"
        schedule()
      }
      const resize = () => schedule()
      const motionChange = () => refresh(true)

      const resizeObserver = new ResizeObserver(resize)
      const observeItems = () => {
        resizeObserver.disconnect()
        resizeObserver.observe(container)
        container.querySelectorAll<HTMLElement>(itemSelector).forEach((item) => resizeObserver.observe(item))
      }
      const mutationObserver = new MutationObserver((mutations) => {
        if (mutations.some((mutation) => mutation.type === "childList")) observeItems()
        schedule()
      })
      mutationObserver.observe(container, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: [
          "aria-selected", "aria-current", "aria-pressed", "aria-disabled",
          "data-active", "data-state", "data-checked", "data-highlighted", "data-disabled",
          "data-popup-open", "data-selected",
        ],
      })
      observeItems()
      container.addEventListener("pointerover", pointerOver)
      if (hoverMode === "nearest") container.addEventListener("pointermove", pointerMove)
      container.addEventListener("pointerleave", pointerLeave)
      container.addEventListener("focusin", focusIn)
      container.addEventListener("focusout", focusOut)
      container.addEventListener("keydown", keyDown)
      container.addEventListener("scroll", resize, { passive: true })
      window.addEventListener("resize", resize)
      reducedMotion.addEventListener("change", motionChange)
      refresh(true)
      schedule(true) // Child refs can attach after the group's callback ref.

      cleanupRef.current = () => {
        cancelAnimationFrame(frame)
        mutationObserver.disconnect()
        resizeObserver.disconnect()
        container.removeEventListener("pointerover", pointerOver)
        if (hoverMode === "nearest") container.removeEventListener("pointermove", pointerMove)
        container.removeEventListener("pointerleave", pointerLeave)
        container.removeEventListener("focusin", focusIn)
        container.removeEventListener("focusout", focusOut)
        container.removeEventListener("keydown", keyDown)
        container.removeEventListener("scroll", resize)
        window.removeEventListener("resize", resize)
        reducedMotion.removeEventListener("change", motionChange)
      }
    },
    [itemSelector, selectedSelector, activeSelector, hoverMode]
  )

  return { containerRef, highlightRef }
}

type GlidingHighlightProps = React.HTMLAttributes<HTMLElement> & {
  as?: "div" | "li"
  ref?: React.Ref<HTMLElement>
}

function GlidingHighlight({ as = "div", ref, className, style, ...props }: GlidingHighlightProps) {
  const shared = {
    "data-slot": "gliding-highlight",
    "aria-hidden": true as const,
    className: cn("pointer-events-none absolute top-0 left-0 z-0 rounded-[12px] opacity-0 will-change-[transform,width,height,opacity]", className),
    style: { width: 0, height: 0, opacity: 0, ...style },
    ...props,
  }
  return React.createElement(as, { ...shared, ref })
}

export { GlidingHighlight, useGlidingHighlight }
