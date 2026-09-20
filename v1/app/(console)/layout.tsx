import { AppShell } from "@/components/app-shell"
import { getConsoleContext } from "@/lib/fabric/session"

export const dynamic = "force-dynamic"

export default async function ConsoleLayout({ children }: LayoutProps<"/">) {
  const context = await getConsoleContext()
  return <AppShell context={context}>{children}</AppShell>
}
