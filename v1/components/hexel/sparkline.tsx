"use client"

import * as React from "react"
import { cn } from "cn"

/**
 * A trend at a glance, for stat tiles and table cells. Plain SVG, no chart
 * library: a 1.5px line in the de-emphasis grey with a 10% wash under it and
 * the latest point marked in `color` (the chart blue by default), ringed in
 * the surface colour, over a soft gradient wash. The line is decorative: put the number it summarises
 * beside it in text.
 */
function Sparkline({
  data,
  width = 96,
  height = 28,
  color = "var(--chart-accent)",
  line = "var(--chart-2)",
  area = true,
  className,
  ...props
}: Omit<React.ComponentProps<"svg">, "color"> & {
  data: number[]
  width?: number
  height?: number
  color?: string
  line?: string
  area?: boolean
}) {
  const wash = React.useId().replace(/:/g, "")
  if (data.length < 2) return null
  const pad = 3
  const min = Math.min(...data)
  const max = Math.max(...data)
  const span = max - min || 1
  const x = (i: number) => pad + (i / (data.length - 1)) * (width - pad * 2)
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - pad * 2)
  const points = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`)
  const path = `M${points.join("L")}`
  const last = data.length - 1
  return (
    <svg
      data-slot="sparkline"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      aria-hidden="true"
      className={cn("shrink-0 overflow-visible", className)}
      {...props}
    >
      {area && (
        <>
          <defs>
            <linearGradient id={wash} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.24" />
              <stop offset="100%" stopColor={color} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path
            d={`${path}L${x(last).toFixed(1)},${height}L${x(0).toFixed(1)},${height}Z`}
            fill={`url(#${wash})`}
          />
        </>
      )}
      <path
        d={path}
        fill="none"
        stroke={line}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
      <circle
        cx={x(last)}
        cy={y(data[last])}
        r="3"
        fill={color}
        stroke="var(--card)"
        strokeWidth="2"
      />
    </svg>
  )
}

export { Sparkline }
