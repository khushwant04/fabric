import type {
  Account,
  ApiKey,
  ConsoleContext,
  Deployment,
  DeploymentStatus,
  DeploymentUsage,
  OidcProvider,
  ServicePrincipal,
  Stamp,
} from "@/lib/fabric/types"

const now = new Date()
const minutesAgo = (minutes: number) =>
  new Date(now.getTime() - minutes * 60_000).toISOString()

export const demoAccount: Account = {
  id: "acc_01JFABRICDEMO000000000001",
  slug: "northstar-labs",
  name: "Northstar Labs",
  status: "active",
  is_system: false,
  managed_capacity_enabled: true,
  created_at: "2026-01-08T09:00:00.000Z",
}

export const demoDeployments: Deployment[] = [
  {
    id: "dep_01",
    account_id: demoAccount.id,
    name: "support-copilot",
    model_alias: "llama-3.1-8b-instruct",
    desired_spec: {
      runtime: {
        release: "v0.9.4",
        kernel_mode: "fabric",
        strategy: "least_in_flight",
        max_model_len: 32768,
      },
      replicas: 3,
      resources: { gpu_count: 1, gpu_class: "l4" },
    },
    generation: 12,
    status: "ready",
    created_at: "2026-08-22T12:00:00.000Z",
    updated_at: minutesAgo(12),
  },
  {
    id: "dep_02",
    account_id: demoAccount.id,
    name: "document-extractor",
    model_alias: "mistral-small-3.1",
    desired_spec: {
      runtime: {
        release: "v0.9.4",
        kernel_mode: "auto",
        strategy: "round_robin",
      },
      replicas: 2,
      resources: { gpu_count: 1, gpu_class: "a10g" },
    },
    generation: 4,
    status: "deploying",
    created_at: "2026-09-01T10:00:00.000Z",
    updated_at: minutesAgo(31),
  },
  {
    id: "dep_03",
    account_id: demoAccount.id,
    name: "batch-summarizer",
    model_alias: "qwen-2.5-14b",
    desired_spec: {
      runtime: {
        release: "v0.9.2",
        kernel_mode: "standard",
        strategy: "least_in_flight",
      },
      replicas: 1,
      resources: { gpu_count: 2, gpu_class: "a100" },
    },
    generation: 7,
    status: "degraded",
    created_at: "2026-08-10T08:00:00.000Z",
    updated_at: minutesAgo(58),
  },
]

export const demoStamps: Stamp[] = [
  {
    id: "stamp_01",
    account_id: demoAccount.id,
    name: "production-east",
    mode: "byoi",
    orchestrator: "kubernetes",
    region: "us-east-1",
    status: "active",
    capabilities: {
      allocatable_gpus: 8,
      requested_gpus: 5,
      agent_version: "0.4.2",
      gpus: [{ product: "NVIDIA L4", count: 8 }],
    },
    last_heartbeat_at: minutesAgo(1),
    revoked_at: null,
    created_at: "2026-07-12T08:00:00.000Z",
  },
  {
    id: "stamp_02",
    account_id: demoAccount.id,
    name: "research-cluster",
    mode: "byoi",
    orchestrator: "kubernetes",
    region: "eu-west-1",
    status: "active",
    capabilities: {
      allocatable_gpus: 4,
      requested_gpus: 2,
      agent_version: "0.4.2",
      gpus: [{ product: "NVIDIA A100", count: 4 }],
    },
    last_heartbeat_at: minutesAgo(4),
    revoked_at: null,
    created_at: "2026-08-04T08:00:00.000Z",
  },
  {
    id: "stamp_03",
    account_id: demoAccount.id,
    name: "staging-west",
    mode: "byoi",
    orchestrator: "kubernetes",
    region: "us-west-2",
    status: "inactive",
    capabilities: { allocatable_gpus: 2, requested_gpus: 0 },
    last_heartbeat_at: minutesAgo(94),
    revoked_at: null,
    created_at: "2026-06-18T08:00:00.000Z",
  },
]

export const demoStatuses: DeploymentStatus[] = [
  {
    deployment_id: "dep_01",
    stamp_id: "stamp_01",
    observed_generation: 12,
    phase: "ready",
    ready_replicas: 3,
    unavailable_replicas: 0,
    endpoint: "https://inference.example.com/v1/support-copilot",
    conditions: [],
    reported_at: minutesAgo(2),
  },
]

export const demoUsage: Record<string, DeploymentUsage> = {
  dep_01: {
    deployment_id: "dep_01",
    events: 128401,
    input_tokens: 18430022,
    output_tokens: 3921840,
    first_occurred_at: "2026-09-01T00:00:00.000Z",
    last_occurred_at: minutesAgo(3),
    stamps: [
      {
        stamp_id: "stamp_01",
        events: 128401,
        input_tokens: 18430022,
        output_tokens: 3921840,
      },
    ],
  },
  dep_02: {
    deployment_id: "dep_02",
    events: 48213,
    input_tokens: 5110020,
    output_tokens: 1093200,
    first_occurred_at: "2026-09-06T00:00:00.000Z",
    last_occurred_at: minutesAgo(18),
    stamps: [],
  },
  dep_03: {
    deployment_id: "dep_03",
    events: 8922,
    input_tokens: 2100400,
    output_tokens: 884110,
    first_occurred_at: "2026-08-11T00:00:00.000Z",
    last_occurred_at: minutesAgo(61),
    stamps: [],
  },
}

export const demoPrincipals: ServicePrincipal[] = [
  {
    id: "sp_01",
    account_id: demoAccount.id,
    name: "production-gateway",
    status: "active",
    created_at: "2026-07-01T10:00:00.000Z",
  },
  {
    id: "sp_02",
    account_id: demoAccount.id,
    name: "ci-deployer",
    status: "active",
    created_at: "2026-08-15T10:00:00.000Z",
  },
]

export const demoApiKeys: ApiKey[] = [
  {
    id: "key_01",
    account_id: demoAccount.id,
    name: "CI deployment key",
    principal_type: "service_principal",
    principal_id: "sp_02",
    key_prefix: "fab_live_7X2A",
    scopes: ["deployments:read", "deployments:write", "stamps:read"],
    expires_at: "2026-12-31T00:00:00.000Z",
    revoked_at: null,
    created_at: "2026-08-15T10:04:00.000Z",
  },
  {
    id: "key_02",
    account_id: demoAccount.id,
    name: "Local development",
    principal_type: "user",
    principal_id: "user_demo",
    key_prefix: "fab_test_2H9Q",
    scopes: ["deployments:read", "inference:invoke"],
    expires_at: null,
    revoked_at: null,
    created_at: "2026-09-02T07:45:00.000Z",
  },
]

export const demoOidcProvider: OidcProvider = {
  id: "oidc_01",
  account_id: demoAccount.id,
  issuer: "https://login.example.com/",
  jwks_uri: "https://login.example.com/.well-known/jwks.json",
  audience: "fabric-console",
  subject_claim: "sub",
  email_claim: "email",
  auto_provision_role: "viewer",
  status: "active",
  created_at: "2026-07-10T10:00:00.000Z",
  updated_at: "2026-08-01T10:00:00.000Z",
}

export const demoConsoleContext: ConsoleContext = {
  account: demoAccount,
  identity: {
    account_id: demoAccount.id,
    principal_type: "user",
    principal_id: "user_demo",
    scopes: [
      "accounts:read",
      "members:read",
      "members:write",
      "api-keys:read",
      "api-keys:write",
      "deployments:read",
      "deployments:write",
      "stamps:read",
      "stamps:write",
    ],
    audience: "fabric-control",
  },
  me: {
    user: {
      id: "user_demo",
      auth0_subject: "auth0|demo",
      email: "operator@northstar.example",
      display_name: "Avery Morgan",
    },
    memberships: [
      {
        account_id: demoAccount.id,
        user_id: "user_demo",
        role: "owner",
        status: "active",
      },
    ],
  },
  demo: true,
}
