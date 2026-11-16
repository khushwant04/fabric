"use client"

import Link from "next/link"
import { useActionState, useCallback, useRef, useState, type ComponentProps, type ReactNode, type RefObject } from "react"
import { CircleAlertIcon, XIcon, type LucideIcon } from "lucide-react"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Drawer, DrawerContent, DrawerDescription, DrawerTitle } from "@/components/ui/drawer"
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet"
import { Spinner } from "@/components/ui/spinner"
import { useIsMobile } from "@/hooks/use-mobile"
import { closeDecision, FORM_DRAWER_WIDTHS, opensInPlace, showsSuccess, type FormActionState, type FormDrawerSize } from "@/lib/form-drawer"
import { cn } from "@/lib/utils"

// Forms in the console open in drawers: a right-side sheet on desktop and a bottom drawer
// below the mobile breakpoint. Both are modal base-ui dialogs, so focus is trapped while
// open, Escape and the backdrop request a close, and focus returns to the trigger.

type CloseRequest = (force?: boolean) => void

/** The responsive shell: header, then children (normally a FormDrawerBody and FormDrawerFooter). */
export function DrawerPanel({ open, onRequestClose, onOpenChangeComplete, size = "default", title, description, icon: Icon, finalFocus, closable = true, children }: {
  open: boolean
  onRequestClose: CloseRequest
  /** Called after the enter or exit transition has finished. */
  onOpenChangeComplete?: (open: boolean) => void
  size?: FormDrawerSize
  title: ReactNode
  description?: ReactNode
  icon?: LucideIcon
  finalFocus?: RefObject<HTMLElement | null>
  /** False while a submission is pending: the close button is disabled. */
  closable?: boolean
  children: ReactNode
}) {
  const mobile = useIsMobile()
  const Title = mobile ? DrawerTitle : SheetTitle
  const Description = mobile ? DrawerDescription : SheetDescription
  const header = <div data-slot="form-drawer-header" className="flex min-w-0 shrink-0 items-start gap-3 border-b border-border/60 px-5 pt-4 pb-4 sm:px-6 sm:pt-5">
    {Icon ? <span aria-hidden="true" className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-none bg-muted text-muted-foreground"><Icon className="size-4" /></span> : null}
    <div className="min-w-0 flex-1">
      <Title className="text-[15px] leading-6 font-medium tracking-[-0.01em]">{title}</Title>
      {description ? <Description className="mt-0.5 text-[13px] leading-5 text-pretty">{description}</Description> : null}
    </div>
    <Button type="button" variant="ghost" size="icon-sm" className="-mt-0.5 -mr-1.5" aria-label="Close" disabled={!closable} onClick={() => onRequestClose()}><XIcon /></Button>
  </div>
  const onOpenChange = (next: boolean) => { if (!next) onRequestClose() }

  if (mobile) {
    return <Drawer open={open} onOpenChange={onOpenChange} onOpenChangeComplete={onOpenChangeComplete} showSwipeHandle>
      <DrawerContent finalFocus={finalFocus} data-form-drawer={size} className="[--drawer-content-max-height:calc(100dvh-1.5rem)]! motion-reduce:transition-none">
        <div className="flex min-h-0 flex-1 flex-col">{header}{children}</div>
      </DrawerContent>
    </Drawer>
  }
  return <Sheet open={open} onOpenChange={onOpenChange} onOpenChangeComplete={onOpenChangeComplete}>
    <SheetContent
      side="right"
      showCloseButton={false}
      finalFocus={finalFocus}
      data-form-drawer={size}
      className={cn("w-full gap-0 border-border/60 bg-popover p-0 shadow-dialog outline-none data-[side=right]:w-full motion-reduce:transition-none", FORM_DRAWER_WIDTHS[size])}
    >
      {header}
      {children}
    </SheetContent>
  </Sheet>
}

/** The scrollable middle of a drawer. */
export function FormDrawerBody({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="form-drawer-body" className={cn("min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain px-5 py-5 sm:px-6", className)} {...props} />
}

/** The footer stays pinned to the bottom of the drawer while the body scrolls. */
export function FormDrawerFooter({ className, error, children, ...props }: ComponentProps<"div"> & { error?: ReactNode }) {
  return <div data-slot="form-drawer-footer" className={cn("shrink-0 border-t border-border/60 bg-popover px-5 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-6", className)} {...props}>
    {error ? <p role="alert" className="mb-3 flex items-start gap-2 rounded-xl bg-destructive/8 px-3 py-2 text-[13px] leading-5 text-destructive"><CircleAlertIcon className="mt-0.5 size-4 shrink-0" /><span className="min-w-0 [overflow-wrap:anywhere]">{error}</span></p> : null}
    <div className="flex min-w-0 items-center justify-end gap-2">{children}</div>
  </div>
}

/** Asked before a drawer with edited input is closed. */
export type DiscardCopy = { title: string; description: string }

const defaultDiscardCopy: DiscardCopy = { title: "Discard changes?", description: "The values you entered in this form will be lost." }

export function DiscardChangesDialog({ open, onKeepEditing, onDiscard, copy = defaultDiscardCopy }: { open: boolean; onKeepEditing: () => void; onDiscard: () => void; copy?: DiscardCopy }) {
  return <AlertDialog open={open} onOpenChange={(next) => { if (!next) onKeepEditing() }}>
    <AlertDialogContent size="sm">
      <AlertDialogHeader>
        <AlertDialogTitle>{copy.title}</AlertDialogTitle>
        <AlertDialogDescription>{copy.description}</AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>Keep editing</AlertDialogCancel>
        <AlertDialogAction variant="destructive" onClick={onDiscard}>Discard</AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
}

/**
 * Open state, close guarding and session reset for a drawer. Each opening gets a fresh
 * `session` key, so any state keyed by it (server-action results, one-time secrets) is
 * cleared once the drawer has closed.
 */
export function useFormDrawer() {
  const [open, setOpen] = useState(false)
  const [session, setSession] = useState(0)
  const triggerRef = useRef<HTMLElement | null>(null)
  const onOpenChangeComplete = useCallback((next: boolean) => { if (!next) setSession((value) => value + 1) }, [])
  return { open, setOpen, session, triggerRef, onOpenChangeComplete }
}

/** A close request that blocks while pending and confirms when the form is dirty. */
export function useGuardedClose({ pending, dirty, succeeded = false, close, copy }: { pending: boolean; dirty: boolean; succeeded?: boolean; close: () => void; copy?: DiscardCopy }) {
  const [confirming, setConfirming] = useState(false)
  const request: CloseRequest = (force = false) => {
    const decision = closeDecision({ pending, dirty, succeeded, force })
    if (decision === "close") close()
    else if (decision === "confirm") setConfirming(true)
  }
  const confirm = <DiscardChangesDialog open={confirming} onKeepEditing={() => setConfirming(false)} onDiscard={() => { setConfirming(false); close() }} copy={copy} />
  return { request, confirm }
}

export type FormDrawerTrigger = {
  label: ReactNode
  icon?: LucideIcon
  disabled?: boolean
  variant?: ComponentProps<typeof Button>["variant"]
  /** Render the trigger as a link; a plain click opens the drawer, other clicks follow it. */
  href?: string
}

export function DrawerTriggerButton({ trigger, triggerRef, onOpen }: { trigger: FormDrawerTrigger; triggerRef: RefObject<HTMLElement | null>; onOpen: () => void }) {
  const Icon = trigger.icon
  const content = <>{Icon ? <Icon /> : null}{trigger.label}</>
  if (trigger.href && !trigger.disabled) {
    return <Button
      ref={triggerRef as RefObject<HTMLButtonElement | null>}
      variant={trigger.variant}
      nativeButton={false}
      aria-haspopup="dialog"
      render={<Link href={trigger.href} />}
      onClick={(event) => { if (opensInPlace(event)) { event.preventDefault(); onOpen() } }}
    >{content}</Button>
  }
  return <Button ref={triggerRef as RefObject<HTMLButtonElement | null>} type="button" variant={trigger.variant} disabled={trigger.disabled} aria-haspopup="dialog" onClick={onOpen}>{content}</Button>
}

export type FormDrawerContext<S> = {
  state: S
  pending: boolean
  /** Report edits from controls that do not emit native input events (Select, Checkbox). */
  markDirty: () => void
}

type FormDrawerProps<S extends FormActionState> = {
  trigger: FormDrawerTrigger
  title: ReactNode
  description?: ReactNode
  icon?: LucideIcon
  size?: FormDrawerSize
  action: (state: S, formData: FormData) => Promise<S>
  initialState?: S
  submitLabel: ReactNode
  pendingLabel: ReactNode
  submitIcon?: LucideIcon
  submitDisabled?: boolean
  /** Shown in place of the form after the action succeeds, with a Done button. */
  renderSuccess?: (state: S) => ReactNode
  children: (context: FormDrawerContext<S>) => ReactNode
}

/** A trigger button plus a drawer holding one server-action form. */
export function FormDrawer<S extends FormActionState>(props: FormDrawerProps<S>) {
  const drawer = useFormDrawer()
  return <>
    <DrawerTriggerButton trigger={props.trigger} triggerRef={drawer.triggerRef} onOpen={() => drawer.setOpen(true)} />
    <FormDrawerSession key={drawer.session} {...props} open={drawer.open} setOpen={drawer.setOpen} triggerRef={drawer.triggerRef} onOpenChangeComplete={drawer.onOpenChangeComplete} />
  </>
}

function FormDrawerSession<S extends FormActionState>({ open, setOpen, triggerRef, onOpenChangeComplete, title, description, icon, size, action, initialState, submitLabel, pendingLabel, submitIcon: SubmitIcon, submitDisabled = false, renderSuccess, children }: FormDrawerProps<S> & {
  open: boolean
  setOpen: (open: boolean) => void
  triggerRef: RefObject<HTMLElement | null>
  onOpenChangeComplete: (open: boolean) => void
}) {
  const [state, formAction, pending] = useActionState<S, FormData>(action, (initialState ?? {}) as Awaited<S>)
  const [dirty, setDirty] = useState(false)
  const markDirty = useCallback(() => setDirty(true), [])
  const succeeded = showsSuccess(state, !!renderSuccess)
  const { request, confirm } = useGuardedClose({ pending, dirty, succeeded, close: () => setOpen(false) })

  return <>
    <DrawerPanel open={open} onRequestClose={request} onOpenChangeComplete={onOpenChangeComplete} size={size} title={title} description={description} icon={icon} finalFocus={triggerRef} closable={!pending}>
      {succeeded && renderSuccess ? <>
        <FormDrawerBody>{renderSuccess(state)}</FormDrawerBody>
        <FormDrawerFooter><Button type="button" onClick={() => request(true)}>Done</Button></FormDrawerFooter>
      </> : <form action={formAction} onInput={markDirty} onChange={markDirty} className="flex min-h-0 min-w-0 flex-1 flex-col">
        <FormDrawerBody>
          <fieldset disabled={pending} className="m-0 min-w-0 border-0 p-0">{children({ state, pending, markDirty })}</fieldset>
        </FormDrawerBody>
        <FormDrawerFooter error={state.error}>
          <Button type="button" variant="outline" disabled={pending} onClick={() => request()}>Cancel</Button>
          <Button type="submit" disabled={pending || submitDisabled}>{pending ? <Spinner /> : SubmitIcon ? <SubmitIcon /> : null}{pending ? pendingLabel : submitLabel}</Button>
        </FormDrawerFooter>
      </form>}
    </DrawerPanel>
    {confirm}
  </>
}
