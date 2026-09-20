export type Account = {
  id: string
  slug: string
  name: string
  status: string
  is_system: boolean
  managed_capacity_enabled: boolean
  created_at: string
}

export type Membership = {
  account_id: string
  user_id: string
  role: "owner" | "admin" | "developer" | "viewer" | string
  status: string
}

export type FabricUser = {
  id: string
  auth0_subject: string
  email: string | null
  display_name: string | null
}

export type Me = { user: FabricUser; memberships: Membership[] }

export type FabricSelf = {
  account_id: string
  principal_type: string
  principal_id: string | null
  scopes: string[]
  audience: string
}

export type RuntimeSpec = {
  release: string
  kernel_mode: "auto" | "fabric" | "standard"
  strategy: "least_in_flight" | "round_robin" | "session_affinity" | "weighted"
  max_model_len?: number | null
  max_num_seqs?: number | null
  gpu_memory_utilization?: number | null
  execution?: "eager" | "cuda_graph" | null
}

export type DeploymentSpec = {
  runtime: RuntimeSpec
  replicas: number
  resources: { gpu_count: number; gpu_class: string }
  limits_policy_ref?: string | null
}

export type Deployment = {
  id: string
  account_id: string
  name: string
  model_alias: string
  desired_spec: DeploymentSpec
  generation: number
  status: string
  created_at: string
  updated_at: string
}

export type Placement = {
  id: string
  account_id: string
  deployment_id: string
  stamp_id: string
  desired_generation: number
  observed_generation: number | null
  status: string
}

export type DeploymentStatus = {
  deployment_id: string
  stamp_id: string
  observed_generation: number | null
  phase: string
  ready_replicas: number
  unavailable_replicas: number
  endpoint: string | null
  conditions: Array<Record<string, unknown>>
  reported_at: string
}

export type DeploymentUsage = {
  deployment_id: string
  events: number
  input_tokens: number
  output_tokens: number
  first_occurred_at: string | null
  last_occurred_at: string | null
  stamps: Array<{
    stamp_id: string
    events: number
    input_tokens: number
    output_tokens: number
  }>
}

export type Stamp = {
  id: string
  account_id: string
  name: string
  mode: string
  orchestrator: string | null
  region: string | null
  status: string
  capabilities: Record<string, unknown>
  last_heartbeat_at: string | null
  revoked_at: string | null
  created_at: string
}

export type ServicePrincipal = {
  id: string
  account_id: string
  name: string
  status: string
  created_at: string
}

export type ApiKey = {
  id: string
  account_id: string
  name: string
  principal_type: string
  principal_id: string
  key_prefix: string
  scopes: string[]
  expires_at: string | null
  revoked_at: string | null
  created_at: string
}

export type OidcProvider = {
  id: string
  account_id: string
  issuer: string
  jwks_uri: string
  audience: string
  subject_claim: string
  email_claim: string
  auto_provision_role: string | null
  status: string
  created_at: string
  updated_at: string
}

export type TokenResponse = {
  access_token: string
  token_type: "Bearer"
  expires_in: number
  account_id: string
  scope: string
}

export type ConsoleContext = {
  account: Account
  identity: FabricSelf
  me: Me
  demo: boolean
}
