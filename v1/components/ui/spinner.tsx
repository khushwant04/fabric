import { cn } from "cn"
import type { ComponentProps } from "react"

export type SpinnerVariant = "default" | "ring" | "orbit" | "orbit-soft" | "orbit-short" | "sweep" | "chase" | "dots" | "pulse"

// Change this setting to "sweep" to update every unqualified <Spinner />.
export const DEFAULT_SPINNER_VARIANT: "sweep" | "orbit-short" = "orbit-short"

function arcPath(start: number, end: number) {
  const point = (angle: number) => {
    const radians = (angle - 90) * Math.PI / 180
    return `${(12 + 9 * Math.cos(radians)).toFixed(4)} ${(12 + 9 * Math.sin(radians)).toFixed(4)}`
  }
  return `M${point(start)} A9 9 0 0 1 ${point(end)}`
}

function OrbitTail({ soft = false, short = false }: { soft?: boolean; short?: boolean }) {
  const length = short ? 110 : soft ? 180 : 145
  const head = short ? 12 : soft ? 10 : 16
  const steps = 36
  const tail = length - head

  return (
    <>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity={soft ? "0.06" : "0.09"} strokeWidth="2" />
      {Array.from({ length: steps }, (_, index) => (
        <path
          key={index}
          d={arcPath(-length + index * tail / steps, -length + (index + 1) * tail / steps + 0.2)}
          stroke="currentColor"
          strokeWidth="2"
          strokeOpacity={0.015 + Math.pow((index + 1) / steps, soft ? 1.8 : 2.7) * (soft ? 0.8 : 0.985)}
        />
      ))}
      <path d={arcPath(-head, 0)} stroke="currentColor" strokeWidth="2" strokeOpacity={soft ? "0.85" : "1"} strokeLinecap="round" />
    </>
  )
}

function Spinner({
  className,
  variant = DEFAULT_SPINNER_VARIANT,
  ...props
}: ComponentProps<"svg"> & { variant?: SpinnerVariant }) {
  const animation = variant === "default" ? DEFAULT_SPINNER_VARIANT : variant === "ring" ? "orbit" : variant
  return (
    <svg
      data-slot="spinner"
      data-variant={animation}
      role="status"
      aria-label="Loading"
      viewBox="0 0 24 24"
      fill="none"
      className={cn("hexel-spinner", variant === "ring" ? "size-8" : "size-4", className)}
      {...props}
    >
      {animation === "orbit" || animation === "orbit-soft" || animation === "orbit-short" ? (
        <OrbitTail soft={animation === "orbit-soft"} short={animation === "orbit-short"} />
      ) : animation === "sweep" ? (
        <>
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.1" strokeWidth="2" />
          <circle className="hexel-spinner-sweep" cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeDasharray="12 57" transform="rotate(-90 12 12)" />
        </>
      ) : animation === "dots" ? (
        [5, 12, 19].map((cx, index) => <circle key={cx} className={`hexel-spinner-dot hexel-spinner-dot-${index}`} cx={cx} cy="12" r="2.25" fill="currentColor" />)
      ) : animation === "pulse" ? (
        <>
          <circle cx="12" cy="12" r="2" fill="currentColor" opacity="0.55" />
          <circle className="hexel-spinner-pulse" cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.6" opacity="0.5" />
          <circle className="hexel-spinner-pulse hexel-spinner-pulse-delayed" cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.6" opacity="0.25" />
        </>
      ) : (
        Array.from({ length: 12 }, (_, index) => (
          <path
            key={index}
            d="M12 3.25v3.5"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            opacity={0.08 + Math.pow(index / 11, 3) * 0.92}
            transform={`rotate(${index * 30} 12 12)`}
          />
        ))
      )}
    </svg>
  )
}

export { Spinner }
