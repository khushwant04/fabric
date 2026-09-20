import type { NextRequest } from "next/server"
import { NextResponse } from "next/server"

import { auth0 } from "@/lib/auth0"

export async function proxy(request: NextRequest) {
  if (!auth0) {
    return NextResponse.next()
  }

  const response = await auth0.middleware(request)

  // Refresh Auth0 credentials at the network boundary so a Server Component can
  // safely exchange them for a short-lived Fabric token without losing rotation.
  if (request.nextUrl.pathname.startsWith("/auth/logout")) {
    response.cookies.set("fabric_account_id", "", {
      httpOnly: true,
      maxAge: 0,
      path: "/",
      sameSite: "lax",
      secure: process.env.NODE_ENV === "production",
    })
  } else if (
    request.nextUrl.pathname.startsWith("/dashboard") ||
    request.nextUrl.pathname.startsWith("/deployments") ||
    request.nextUrl.pathname.startsWith("/stamps") ||
    request.nextUrl.pathname.startsWith("/usage") ||
    request.nextUrl.pathname.startsWith("/access") ||
    request.nextUrl.pathname.startsWith("/settings") ||
    request.nextUrl.pathname.startsWith("/onboarding")
  ) {
    try {
      await auth0.getAccessToken(request, response)
    } catch {
      // Protected layouts perform the authoritative session check and redirect.
    }
  }

  return response
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt).*)",
  ],
}
