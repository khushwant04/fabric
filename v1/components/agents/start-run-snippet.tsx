import { CopyCode } from "@/components/agents/copy-code"

const curl = `curl "$FABRIC_INFERENCE_URL/v1/chat/completions" \\
  -H "Authorization: Bearer $FABRIC_INFERENCE_TOKEN" \\
  -H "Content-Type: application/json" \\
  -H "X-Fabric-Run-Id: task-42" \\
  -H "X-Fabric-Run-Budget-Tokens: 20000" \\
  -H "X-Fabric-Run-Budget-Steps: 8" \\
  --data '{"model": "<model-alias>",
    "messages": [{"role": "user", "content": "Plan a trip"}]}'`

const python = `from openai import OpenAI

client = OpenAI(base_url=f"{FABRIC_INFERENCE_URL}/v1",
                api_key=FABRIC_INFERENCE_TOKEN)
# Send the same run id on every model call of the task.
run = {"X-Fabric-Run-Id": "task-42",
       "X-Fabric-Run-Budget-Tokens": "20000",
       "X-Fabric-Run-Budget-Steps": "8"}

reply = client.chat.completions.create(
    model="<model-alias>",
    messages=[{"role": "user", "content": "Plan a trip"}],
    extra_headers=run)`

export function StartRunSnippet() {
  return <div className="grid w-full gap-3 text-left lg:grid-cols-2">
    <CopyCode label="cURL" code={curl} />
    <CopyCode label="Python · openai SDK" code={python} />
  </div>
}
