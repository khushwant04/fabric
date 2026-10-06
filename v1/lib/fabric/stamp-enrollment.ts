// Keep setup commands independent of browser state and never persist their token.
export const STAMP_CHART_REF = "16ab94aa8e18d9984b90011aa33080f62eaa9cdb"
export const STAMP_REPOSITORY = "https://github.com/khushwant04/fabric"
export const K3S_BOOTSTRAP_URL = "https://raw.githubusercontent.com/khushwant04/fabric/bootstrap-k3s-v1/scripts/bootstrap-k3s.sh"

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
const hostname = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/

export function stampSetupError(setup: StampSetup, requireIssuer = true): string | null {
  if (!dnsLabel.test(setup.stampName) || setup.stampName.length > 53) return "Use a lowercase stamp name with letters, numbers and hyphens, up to 53 characters."
  if (!httpsControlPlaneUrl(setup.controlPlaneUrl)) return "Enter the HTTPS control-plane origin that this cluster can reach."
  if ((requireIssuer || setup.jwtIssuer) && !validIssuer(setup.jwtIssuer)) return "Enter the exact HTTPS JWT issuer configured on the control plane."
  if (setup.target === "kubernetes" && !setup.kubeContext.trim()) return "Enter the kubectl context for the cluster you want to enroll."
  if (setup.runtimeClass && (!dnsLabel.test(setup.runtimeClass) || setup.runtimeClass.length > 63)) return "Use a valid Kubernetes RuntimeClass name."
  if (!setup.modelImage || /\s|[\x00-\x1f]/.test(setup.modelImage)) return "Enter the vLLM container image for this cluster."
  if (setup.gatewayHost && (!hostname.test(setup.gatewayHost) || setup.gatewayHost.length > 253 || setup.gatewayHost.split(".").some((label) => label.length > 63))) return "Enter the inference gateway DNS hostname without https:// or a path."
  if (setup.gatewayHost && !setup.certificateIssuer) return "Enter an existing cert-manager ClusterIssuer for the inference gateway."
  if (setup.certificateIssuer && (!hostname.test(`${setup.certificateIssuer}.local`) || setup.certificateIssuer.length > 253)) return "Use a valid ClusterIssuer name."
  if (setup.ingressClass && (!dnsLabel.test(setup.ingressClass) || setup.ingressClass.length > 63)) return "Use a valid IngressClass name."
  return null
}

export function k3sBootstrapCommand(stampName: string, nvidia: boolean): string | null {
  if (!dnsLabel.test(stampName) || stampName.length > 53) return null
  return [
    `curl --fail --location --proto '=https' ${shellQuote(K3S_BOOTSTRAP_URL)} \\`,
    "  --output fabric-bootstrap-k3s.sh && \\",
    `sudo bash ./fabric-bootstrap-k3s.sh --node-name ${shellQuote(stampName)}${nvidia ? " --nvidia" : ""}`,
  ].join("\n")
}

export function stampHelmCommand(setup: StampSetup, token: string): string | null {
  if (stampSetupError(setup) || !token || /[\r\n\x00]/.test(token)) return null
  const root = `fabric-${STAMP_CHART_REF}`
  const helm = setup.target === "k3s"
    ? "sudo helm --kubeconfig /etc/rancher/k3s/k3s.yaml --kube-context default"
    : `helm --kube-context ${shellQuote(setup.kubeContext.trim())}`
  const options = [
    "--namespace fabric-stamp --create-namespace",
    `--values ${root}/deploy/releases/singletenant-20261006/stamp-images.yaml`,
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
    `curl --fail --location --proto '=https' ${shellQuote(`${STAMP_REPOSITORY}/archive/${STAMP_CHART_REF}.tar.gz`)} \\`,
    `  --output ${root}.tar.gz && \\`,
    `tar -xzf ${root}.tar.gz && \\`,
    `${helm} upgrade --install ${shellQuote(setup.stampName)} ./${root}/deploy/helm/fabric-stamp \\`,
    options.map((option, index) => `  ${option}${index < options.length - 1 ? " \\" : ""}`).join("\n"),
  ].join("\n")
}
