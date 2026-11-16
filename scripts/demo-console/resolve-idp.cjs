// Demo tooling only: preloaded into the console's Node process with
// NODE_OPTIONS=--require so the server can reach the mock identity provider and the demo
// edge gateway by name.
//
// The Auth0 SDK accepts only a bare hostname as its domain (no port, no localhost, no IP
// address) and always talks to https://<domain>/. The demo identity provider therefore uses
// a name of its own (DEMO_IDP_HOST, e.g. idp.fabric-demo.test) whose certificate comes
// from the demo's throwaway CA, and this shim sends connections for that name on port 443
// to where the provider actually listens (DEMO_IDP_ADDR, e.g. 127.0.0.1:8443).
//
// DEMO_HOST_MAP adds more names the same way, as comma-separated name=address:port pairs.
// The demo uses it for the edge gateway's public name (inference.northstar-labs.example),
// so the console shows and calls a realistic gateway URL. TLS is still verified against
// each name, using NODE_EXTRA_CA_CERTS. No other host is affected.
"use strict"

const net = require("node:net")
const tls = require("node:tls")

const targets = new Map()
const [idpAddress, idpPort] = (process.env.DEMO_IDP_ADDR || "").split(":")
if (process.env.DEMO_IDP_HOST && idpAddress && idpPort) {
  targets.set(process.env.DEMO_IDP_HOST.toLowerCase(), { address: idpAddress, port: Number(idpPort) })
}
for (const entry of (process.env.DEMO_HOST_MAP || "").split(",")) {
  const [name, target] = entry.split("=")
  const [address, port] = (target || "").split(":")
  if (name && address && port) targets.set(name.trim().toLowerCase(), { address, port: Number(port) })
}

if (targets.size) {
  const rewrite = (options) => {
    if (!options || typeof options !== "object") return options
    const name = String(options.host || options.hostname || options.servername || "").toLowerCase()
    const target = targets.get(name)
    if (!target || Number(options.port || 443) !== 443) return options
    return { ...options, host: target.address, hostname: target.address, port: target.port, servername: name }
  }
  const wrap = (original) => function connect(...args) {
    if (args[0] && typeof args[0] === "object") args[0] = rewrite(args[0])
    else if (typeof args[0] === "number" && typeof args[1] === "string" &&
             targets.has(args[1].toLowerCase()) && args[0] === 443) {
      const options = rewrite({ port: args[0], host: args[1], ...(typeof args[2] === "object" ? args[2] : {}) })
      return original.call(this, options, ...args.slice(typeof args[2] === "object" ? 3 : 2))
    }
    return original.apply(this, args)
  }
  tls.connect = wrap(tls.connect)
  net.connect = wrap(net.connect)
  net.createConnection = wrap(net.createConnection)
}
