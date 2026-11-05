# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub security advisories](https://github.com/khushwant04/fabric/security/advisories/new),
not in a public issue or pull request.

Include the affected component and version (or commit), steps to reproduce, and the
impact you observed. You should receive an acknowledgement within five working days.

## Scope

Of particular interest:

- crossing tenant boundaries: reading or writing another account's data, deployments
  or usage, including through row-level-security gaps;
- forging, replaying or escalating Fabric tokens, API keys, or stamp enrollment,
  agent or telemetry credentials;
- reaching a model host without passing data-plane authorization;
- credentials or prompts appearing in logs, metrics labels or stored usage;
- a stamp agent or collector being able to act beyond its own stamp.

The security model these are measured against is described in
[Security and identity](docs/context/security-identity.md).

## Supported versions

Fabric has not yet published a tagged release. Fixes land on `main`.
