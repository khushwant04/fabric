// Keep setup commands independent of browser state and never persist their token.
// One release pins the chart and every image: CI publishes images for each commit on main.
export const STAMP_RELEASE = "57b0fdb73e6de28d020a5530cc42ac5ce38defba"
export const STAMP_IMAGE_REGISTRY = "public.ecr.aws/hexelstudio/fabric"
export const STAMP_REPOSITORY = "https://github.com/khushwant04/fabric"
export const STAMP_BOOTSTRAP_URL = "https://raw.githubusercontent.com/khushwant04/fabric/main/scripts/bootstrap-stamp.sh"
export const STAMP_MODEL_IMAGE = `${STAMP_IMAGE_REGISTRY}/model-host:git-${STAMP_RELEASE}`

export type StampEnrollmentConfig = {
  controlPlaneUrl: string
  jwtIssuer: string
}

export type StampSetup = {
  target: "k3s" | "kubernetes"
  stampName: string
  controlPlaneUrl: string
  jwtIssuer: string
  kubeContext: string
  runtimeClass: string
  modelImage: string
  gatewayHost: string
  ingressClass: string
  certificateIssuer: string
  /** Let's Encrypt contact for the public hostname on a k3s VM. */
  acmeEmail: string
}

export function httpsControlPlaneUrl(raw: string): string | null {
  try {
    const url = new URL(raw.trim())
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") return null
    if (url.hostname === "localhost" || url.hostname.endsWith(".localhost") || url.hostname === "127.0.0.1" || url.hostname === "[::1]" ||
      url.hostname.endsWith(".svc") || url.hostname.endsWith(".svc.cluster.local")) return null
    return url.origin
  } catch { return null }
}

function validIssuer(raw: string): boolean {
  try {
    const url = new URL(raw)
    return url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash
  } catch { return false }
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`
}

function helmString(key: string, value: string): string {
  // Helm's --set parser treats commas and backslashes specially, independently of bash.
  const escaped = value.replaceAll("\\", "\\\\").replaceAll(",", "\\,")
  return `--set-string ${key}=${shellQuote(escaped)}`
}

const dnsLabel = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/
const email = /^[^@\s'"]+@[^@\s'"]+\.[^@\s'"]+$/
const hostname = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/

export function stampSetupError(setup: StampSetup, requireIssuer = true): string | null {
  if (!dnsLabel.test(setup.stampName) || setup.stampName.length > 53) return "Use a lowercase stamp name with letters, numbers and hyphens, up to 53 characters."
  if (!httpsControlPlaneUrl(setup.controlPlaneUrl)) return "Enter the HTTPS control-plane origin that this cluster can reach."
  if ((requireIssuer || setup.jwtIssuer) && !validIssuer(setup.jwtIssuer)) return "Enter the exact HTTPS JWT issuer configured on the control plane."
  if (setup.target === "kubernetes" && !setup.kubeContext.trim()) return "Enter the kubectl context for the cluster you want to enroll."
  if (setup.runtimeClass && (!dnsLabel.test(setup.runtimeClass) || setup.runtimeClass.length > 63)) return "Use a valid Kubernetes RuntimeClass name."
  if (!setup.modelImage || /\s|[\x00-\x1f]/.test(setup.modelImage)) return "Enter the vLLM container image for this cluster."
  if (setup.gatewayHost && (!hostname.test(setup.gatewayHost) || setup.gatewayHost.length > 253 || setup.gatewayHost.split(".").some((label) => label.length > 63))) return "Enter the inference gateway DNS hostname without https:// or a path."
  // The stamp's own name in the control-plane field produces a stamp that never enrolls.
  if (setup.gatewayHost && httpsControlPlaneUrl(setup.controlPlaneUrl) === `https://${setup.gatewayHost}`) return "The control-plane address is the Fabric API this stamp joins, not the stamp's own hostname. Keep the console's control-plane address here and put the stamp hostname under Runtime & gateway."
  if (setup.gatewayHost && setup.target === "k3s" && !email.test(setup.acmeEmail)) return "Enter a contact email for the Let's Encrypt certificate."
  if (setup.gatewayHost && setup.target === "kubernetes" && !setup.certificateIssuer) return "Enter an existing cert-manager ClusterIssuer for the inference gateway."
  if (setup.certificateIssuer && (!hostname.test(`${setup.certificateIssuer}.local`) || setup.certificateIssuer.length > 253)) return "Use a valid ClusterIssuer name."
  if (setup.ingressClass && (!dnsLabel.test(setup.ingressClass) || setup.ingressClass.length > 63)) return "Use a valid IngressClass name."
  return null
}

/** The whole k3s VM setup in one command: k3s, GPU runtime, TLS, enrollment and the stamp. */
export function stampBootstrapCommand(setup: StampSetup, token: string, nvidia: boolean): string | null {
  if (setup.target !== "k3s" || stampSetupError(setup) || !token || /[\r\n\x00]/.test(token)) return null
  const controlPlane = httpsControlPlaneUrl(setup.controlPlaneUrl)!
  const options = [
    `--name ${shellQuote(setup.stampName)}`,
    `--control-plane ${shellQuote(controlPlane)}`,
    `--token ${shellQuote(token)}`,
  ]
  if (setup.jwtIssuer && setup.jwtIssuer.replace(/\/$/, "") !== controlPlane) options.push(`--issuer ${shellQuote(setup.jwtIssuer)}`)
  if (setup.gatewayHost) options.push(`--host ${shellQuote(setup.gatewayHost)}`, `--email ${shellQuote(setup.acmeEmail)}`)
  if (!nvidia) options.push("--no-gpu")
  options.push(`--release ${STAMP_RELEASE}`)
  return [
    `curl -fsSL ${shellQuote(STAMP_BOOTSTRAP_URL)} | sudo bash -s -- \\`,
    options.map((option, index) => `  ${option}${index < options.length - 1 ? " \\" : ""}`).join("\n"),
  ].join("\n")
}

export function stampHelmCommand(setup: StampSetup, token: string): string | null {
  if (stampSetupError(setup) || !token || /[\r\n\x00]/.test(token)) return null
  const root = `fabric-${STAMP_RELEASE}`
  const tag = `git-${STAMP_RELEASE}`
  const helm = setup.target === "k3s"
    ? "sudo helm --kubeconfig /etc/rancher/k3s/k3s.yaml --kube-context default"
    : `helm --kube-context ${shellQuote(setup.kubeContext.trim())}`
  const options = [
    "--namespace fabric-stamp --create-namespace",
    helmString("image.agent.repository", `${STAMP_IMAGE_REGISTRY}/agent`),
    helmString("image.agent.tag", tag),
    helmString("image.dataPlane.repository", `${STAMP_IMAGE_REGISTRY}/data-plane`),
    helmString("image.dataPlane.tag", tag),
    helmString("stamp.name", setup.stampName),
    helmString("stamp.orchestrator", setup.target),
    helmString("controlPlane.url", httpsControlPlaneUrl(setup.controlPlaneUrl)!),
    helmString("controlPlane.jwtIssuer", setup.jwtIssuer),
    "--set operator.enabled=true",
    helmString("operator.managedModelHost.image", setup.modelImage),
    helmString("enrollment.token", token),
  ]
  if (setup.runtimeClass) options.push(helmString("gpu.runtimeClassName", setup.runtimeClass))
  if (setup.target === "k3s") options.push(
    helmString("persistence.storageClass", "local-path"),
    helmString("usageSpool.storageClass", "local-path"),
    helmString("operator.managedModelHost.cache.hostPath", "/var/lib/fabric/model-cache"),
  )
  if (setup.gatewayHost) options.push(
    "--set exposure.enabled=true",
    helmString("exposure.host", setup.gatewayHost),
    helmString("exposure.className", setup.ingressClass),
    helmString("exposure.tls.issuer.name", setup.certificateIssuer),
  )
  options.push("--wait --timeout 15m")
  // && keeps a failed download/extraction from running Helm against a stale directory.
  return [
    `curl --fail --location --proto '=https' ${shellQuote(`${STAMP_REPOSITORY}/archive/${STAMP_RELEASE}.tar.gz`)} \\`,
    `  --output ${root}.tar.gz && \\`,
    `tar -xzf ${root}.tar.gz && \\`,
    `${helm} upgrade --install ${shellQuote(setup.stampName)} ./${root}/deploy/helm/fabric-stamp \\`,
    options.map((option, index) => `  ${option}${index < options.length - 1 ? " \\" : ""}`).join("\n"),
  ].join("\n")
}
