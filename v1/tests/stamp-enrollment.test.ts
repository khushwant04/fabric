import { strict as assert } from "node:assert"
import { test } from "node:test"

import {
  STAMP_MODEL_IMAGE,
  STAMP_RELEASE,
  stampBootstrapCommand,
  stampHelmCommand,
  stampSetupError,
  type StampSetup,
} from "../lib/fabric/stamp-enrollment"

const vm: StampSetup = {
  target: "k3s", stampName: "rtx-pro-6000", controlPlaneUrl: "https://api.example.com",
  jwtIssuer: "https://api.example.com", kubeContext: "", runtimeClass: "nvidia",
  modelImage: STAMP_MODEL_IMAGE, gatewayHost: "rtx-pro-6000.stamps.example.com", ingressClass: "traefik",
  certificateIssuer: "", acmeEmail: "ops@example.com",
}

test("a k3s VM gets one setup command carrying everything as parameters", () => {
  const command = stampBootstrapCommand(vm, "tok_123", true)!
  assert.match(command, /^curl -fsSL '.*\/scripts\/bootstrap-stamp\.sh' \| sudo bash -s -- \\/)
  assert.match(command, /--name 'rtx-pro-6000'/)
  assert.match(command, /--control-plane 'https:\/\/api\.example\.com'/)
  assert.match(command, /--token 'tok_123'/)
  assert.match(command, /--host 'rtx-pro-6000\.stamps\.example\.com'/)
  assert.match(command, /--email 'ops@example\.com'/)
  assert.match(command, new RegExp(`--release ${STAMP_RELEASE}$`))
  assert.doesNotMatch(command, /--issuer|--no-gpu/)
})

test("the setup command adds --issuer only when it differs, and --no-gpu without GPUs", () => {
  const command = stampBootstrapCommand({ ...vm, jwtIssuer: "https://issuer.example.com", gatewayHost: "" }, "t", false)!
  assert.match(command, /--issuer 'https:\/\/issuer\.example\.com'/)
  assert.match(command, /--no-gpu/)
  assert.doesNotMatch(command, /--host|--email/)
})

test("a public hostname on k3s needs a certificate contact, not a ClusterIssuer", () => {
  assert.match(stampSetupError({ ...vm, acmeEmail: "" }) ?? "", /contact email/)
  assert.equal(stampSetupError(vm), null)
  assert.equal(stampBootstrapCommand({ ...vm, acmeEmail: "nope" }, "t", true), null)
})

test("tokens and values are shell-quoted, and bad tokens produce no command", () => {
  assert.match(stampBootstrapCommand(vm, "a'b", true)!, /--token 'a'"'"'b'/)
  assert.equal(stampBootstrapCommand(vm, "a\nb", true), null)
  assert.equal(stampBootstrapCommand(vm, "", true), null)
})

test("existing Kubernetes gets a Helm command pinned to the same release and images", () => {
  const cluster: StampSetup = { ...vm, target: "kubernetes", kubeContext: "aks", gatewayHost: "", acmeEmail: "" }
  assert.equal(stampBootstrapCommand(cluster, "t", true), null)
  const helm = stampHelmCommand(cluster, "t")!
  assert.match(helm, new RegExp(`archive/${STAMP_RELEASE}\\.tar\\.gz`))
  assert.match(helm, new RegExp(`image\\.agent\\.tag='git-${STAMP_RELEASE}'`))
  assert.match(helm, /gpu\.runtimeClassName='nvidia'/)
  assert.doesNotMatch(helm, /singletenant-20261006/)
})

test("the stamp's own hostname is refused as the control-plane address", () => {
  const swapped = { ...vm, controlPlaneUrl: "https://rtx-pro-6000.stamps.example.com" }
  assert.match(stampSetupError(swapped) ?? "", /not the stamp's own hostname/)
  assert.equal(stampBootstrapCommand(swapped, "t", true), null)
})
