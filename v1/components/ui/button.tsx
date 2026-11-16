import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"

// Fabric's buttons: rounded-lg and 40px tall by default. One near-black `default` per view
// for the main action; `outline` (white with a hairline border) for everything secondary.
// A press changes only colour: buttons never move.
const buttonVariants = cva(
  "group/button relative inline-flex shrink-0 items-center justify-center rounded-lg font-medium whitespace-nowrap transition-[background-color,color,border-color,box-shadow,opacity] duration-(--motion-fast) ease-strong outline-none select-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 aria-invalid:ring-2 aria-invalid:ring-destructive/40 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground hover:bg-primary/85",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-[color-mix(in_oklch,var(--secondary),var(--foreground)_6%)]",
        outline:
          "border border-border bg-card text-foreground shadow-[0_1px_1px_rgb(0_0_0/0.03)] hover:bg-muted aria-expanded:bg-muted",
        ghost: "text-foreground hover:bg-muted aria-expanded:bg-muted",
        destructive:
          "border border-destructive/20 bg-destructive/8 text-destructive hover:bg-destructive/14 dark:bg-destructive/15 dark:hover:bg-destructive/22",
        "destructive-solid": "bg-destructive text-white hover:bg-destructive/90",
        link: "h-auto! rounded-sm px-0! text-foreground underline decoration-foreground/30 underline-offset-4 hover:decoration-foreground",
      },
      size: {
        xs: "h-7 gap-1 rounded-md px-2.5 text-xs [&_svg:not([class*='size-'])]:size-3.5",
        sm: "h-8 gap-1.5 rounded-md px-3 text-[13px] [&_svg:not([class*='size-'])]:size-3.5",
        default: "h-10 gap-2 px-4 text-sm",
        lg: "h-11 gap-2 px-5 text-[15px]",
        xl: "h-12 gap-2 px-6 text-base [&_svg:not([class*='size-'])]:size-[18px]",
        icon: "size-10",
        "icon-xs": "size-7 rounded-md [&_svg:not([class*='size-'])]:size-3.5",
        "icon-sm": "size-8 rounded-md",
        "icon-lg": "size-11",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
