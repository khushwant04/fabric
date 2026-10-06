"use client"

import { useCallback, useEffect, useRef, useState } from "react"

/** Keep clipboard feedback local to its control, including repeated clicks. */
export function useCopyFeedback() {
  const [copied, setCopied] = useState(false)
  const resetRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(false)
  const requestRef = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      requestRef.current += 1
      if (resetRef.current !== null) clearTimeout(resetRef.current)
    }
  }, [])

  const copy = useCallback(async (text: string): Promise<boolean> => {
    const request = ++requestRef.current
    try {
      await navigator.clipboard.writeText(text)
      if (!mountedRef.current || request !== requestRef.current) return true
      if (resetRef.current !== null) clearTimeout(resetRef.current)
      setCopied(true)
      resetRef.current = setTimeout(() => {
        resetRef.current = null
        setCopied(false)
      }, 2000)
      return true
    } catch {
      if (mountedRef.current && request === requestRef.current) {
        if (resetRef.current !== null) clearTimeout(resetRef.current)
        resetRef.current = null
        setCopied(false)
      }
      return false
    }
  }, [])

  return { copied, copy }
}
