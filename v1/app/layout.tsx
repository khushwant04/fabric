import type { Metadata } from "next"
import { Geist, Geist_Mono, Inter } from "next/font/google"

import "./globals.css"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ThemeSync } from "@/hooks/use-theme"
import { cn } from "@/lib/utils"

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" })
const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] })
const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
})

export const metadata: Metadata = {
  title: { default: "Fabric Console", template: "%s | Fabric" },
  description: "Operate Fabric deployments, inference stamps, and access.",
}

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={cn(
        "h-full antialiased",
        geistSans.variable,
        geistMono.variable,
        inter.variable
      )}
    >
      <body className="min-h-full bg-background font-sans text-foreground">
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){var t="system";try{var s=localStorage.getItem("fabric-theme");if(s==="light"||s==="dark"||s==="system")t=s}catch(e){}var r="light";if(t==="dark")r="dark";else if(t==="system"){try{if(window.matchMedia("(prefers-color-scheme: dark)").matches)r="dark"}catch(e){}}document.documentElement.classList.toggle("dark",r==="dark");document.documentElement.style.colorScheme=r})()`,
          }}
        />
        <ThemeSync />
        <TooltipProvider>{children}</TooltipProvider>
      </body>
    </html>
  )
}
