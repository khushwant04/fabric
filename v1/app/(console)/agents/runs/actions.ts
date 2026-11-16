"use server"

import { revalidatePath } from "next/cache"
import { redirect, unstable_rethrow } from "next/navigation"

import { deleteAgentRunOnGateway } from "@/lib/fabric/agent-data"
import { AgentRunsError, isRunId } from "@/lib/fabric/agent-runs"

export type DeleteRunState = { error?: string }

export async function deleteAgentRun(runId: string, previous: DeleteRunState): Promise<DeleteRunState> {
  if (!isRunId(runId)) return { error: "That run id is not valid." }
  try {
    await deleteAgentRunOnGateway(runId)
  } catch (error) {
    unstable_rethrow(error)
    if (error instanceof AgentRunsError && error.kind !== "not_found") return { ...previous, error: error.message }
    if (!(error instanceof AgentRunsError)) return { error: "The run could not be deleted. Try again." }
    // Already gone: the outcome the user asked for.
  }
  revalidatePath("/agents/runs")
  redirect("/agents/runs?deleted=1")
}
