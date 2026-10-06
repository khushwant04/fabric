"use server"

import { revalidatePath } from "next/cache"
import { redirect, unstable_rethrow } from "next/navigation"

import { controlPlaneRequest, FabricApiError } from "@/lib/fabric/client"
import { confirmsModelDeletion } from "@/lib/fabric/resource-state"
import { getConsoleContext, getFabricAccessToken, requireScope } from "@/lib/fabric/session"
import type { Deployment } from "@/lib/fabric/types"

export async function confirmDeleteDeployment(
  deploymentId: string,
  _previous: { error?: string },
  formData: FormData
): Promise<{ error?: string }> {
  try {
    const context = await getConsoleContext()
    requireScope(context, "deployments:write")
    if (!/^[a-zA-Z0-9_-]{1,128}$/.test(deploymentId)) return { error: "Choose a valid deployment." }
    const token = await getFabricAccessToken(context.account.id)
    const path = `/v1/accounts/${context.account.id}/deployments/${deploymentId}`
    // Re-read the account-owned resource; the browser cannot supply the name
    // being confirmed or authorize deletion in another selected account.
    const deployment = await controlPlaneRequest<Deployment>(path, { token })
    if (deployment.account_id !== context.account.id || deployment.id !== deploymentId) return { error: "This deployment is unavailable in the selected account." }
    if (!confirmsModelDeletion(formData.get("confirmation"), deployment.model_alias)) return { error: `Type ${deployment.model_alias} exactly to confirm deletion.` }
    await controlPlaneRequest(path, { method: "DELETE", token })
    revalidatePath("/deployments")
    revalidatePath("/dashboard")
  } catch (error) {
    unstable_rethrow(error)
    if (error instanceof FabricApiError && [403, 404].includes(error.status)) {
      return { error: "This deployment is unavailable or you do not have permission to delete it." }
    }
    return { error: "Deletion could not be completed. Try again." }
  }
  redirect("/deployments")
}
