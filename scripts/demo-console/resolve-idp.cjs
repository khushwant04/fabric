// Demo tooling only: preloaded into the console's Node process with
// NODE_OPTIONS=--require so the server can reach the mock identity provider.
//
// The Auth0 SDK accepts only a bare hostname as its domain (no port, no localhost, no IP
// address) and always talks to https://<domain>/. The demo identity provider therefore uses
// a name of its own (DEMO_IDP_HOST, e.g. idp.fabric-demo.test) whose certificate comes
// from the demo's throwaway CA, and this shim sends connections for that name on port 443
// to where the provider actually listens (DEMO_IDP_ADDR, e.g. 127.0.0.1:8443). TLS is still
// verified against that name, using NODE_EXTRA_CA_CERTS. No other host is affected.
"use strict"

const net = require("node:net")
const tls = require("node:tls")

const host = (process.env.DEMO_IDP_HOST || "").toLowerCase()
const [address, port] = (process.env.DEMO_IDP_ADDR || "").split(":")

if (host && address && port) {
  const rewrite = (options) => {
    if (!options || typeof options !== "object") return options
    const target = String(options.host || options.hostname || options.servername || "").toLowerCase()
    if (target !== host || Number(options.port || 443) !== 443) return options
    return { ...options, host: address, hostname: address, port: Number(port), servername: host }
  }
  const wrap = (original) => function connect(...args) {
    if (args[0] && typeof args[0] === "object") args[0] = rewrite(args[0])
    else if (typeof args[0] === "number" && typeof args[1] === "string" &&
             args[1].toLowerCase() === host && args[0] === 443) {
      const options = rewrite({ port: args[0], host: args[1], ...(typeof args[2] === "object" ? args[2] : {}) })
      return original.call(this, options, ...args.slice(typeof args[2] === "object" ? 3 : 2))
    }
    return original.apply(this, args)
  }
  tls.connect = wrap(tls.connect)
  net.connect = wrap(net.connect)
  net.createConnection = wrap(net.createConnection)
}
