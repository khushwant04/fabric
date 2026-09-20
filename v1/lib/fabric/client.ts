import "server-only"

const DEFAULT_CONTROL_PLANE_URL = "http://localhost:8000"

export class FabricApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details?: unknown
  ) {
    super(message)
    this.name = "FabricApiError"
  }
}

type ErrorEnvelope = {
  error?: { code?: string; message?: string; details?: unknown }
  detail?: unknown
}

async function parseFailure(response: Response): Promise<FabricApiError> {
  let payload: ErrorEnvelope | null = null
  try {
    payload = (await response.json()) as ErrorEnvelope
  } catch {
    // Non-JSON upstream failures are intentionally reduced to a safe message.
  }

  const validationMessage = Array.isArray(payload?.detail)
    ? payload.detail
        .map((item) =>
          typeof item === "object" && item && "msg" in item
            ? String(item.msg)
            : "Invalid value"
        )
        .join("; ")
    : null

  return new FabricApiError(
    response.status,
    payload?.error?.code ??
      (response.status === 422 ? "validation_error" : "request_failed"),
    payload?.error?.message ??
      validationMessage ??
      `Control plane request failed (${response.status})`,
    payload?.error?.details ?? payload?.detail
  )
}

export async function controlPlaneRequest<T>(
  path: string,
  init: RequestInit & { token?: string } = {}
): Promise<T> {
  const baseUrl = (
    process.env.FABRIC_CONTROL_PLANE_URL ?? DEFAULT_CONTROL_PLANE_URL
  ).replace(/\/$/, "")
  const headers = new Headers(init.headers)
  headers.set("Accept", "application/json")
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json")
  }
  if (init.token) headers.set("Authorization", `Bearer ${init.token}`)

  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  })

  if (!response.ok) throw await parseFailure(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}
