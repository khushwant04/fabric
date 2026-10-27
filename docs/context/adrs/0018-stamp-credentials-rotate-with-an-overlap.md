# ADR 0018: Stamp credentials rotate from the stamp, with an overlap

**Decision status:** Accepted  
**Implementation status:** Implemented in control plane, agent, collector and chart  
**Date:** 2026-10-27

## Context

Enrollment gives a stamp two long-lived machine credentials: the agent's, which reads desired
state and writes status, and the collector's write-only telemetry credential (ADR 0008). Nothing
replaced them afterwards. `credential_version` existed on the stamp and on each credential row and
was never written. A credential that cannot be rotated is replaced only by revoking the stamp and
enrolling again, which needs a new single-use token from an operator and loses the stamp's identity.

Two constraints shape the answer:

- **Outbound only.** The control plane never connects into a customer cluster (ADR 0001), so it
  cannot push new credentials. The stamp has to fetch them.
- **Two processes, two files.** The agent persists its identity in `credentials.json`; the
  collector reads only its own telemetry file. They do not change at the same instant.

## Decision

### 1. The agent rotates, authorized by the credential it is replacing

`POST /v1/stamps/{id}/credentials/rotate` mints a new agent and telemetry credential pair at the
stamp's next `credential_version`. It requires the heartbeat scope, which every agent credential
has held since enrollment, so existing credentials can rotate without first being reissued. The
telemetry credential cannot rotate anything.

### 2. Old credentials overlap instead of being revoked

Every earlier unrevoked credential of the stamp, of both kinds, is given an expiry at most
`stamp_credential_rotation_grace_seconds` away (one hour by default) rather than revoked at once.
That window covers the collector picking up its new file. It also covers the one failure rotation
must survive, a lost reply: the agent still holds a working credential and simply rotates again.
The second rotation shortens the credentials from the lost reply as well, so a lost reply cannot
leave a long-lived credential that nobody holds.

A suspected leak is not a rotation. Revoking the stamp still takes effect immediately.

### 3. Persist before use

The agent writes the new pair to `credentials.json` before presenting it, and writes the
collector's file last. If persisting fails, the agent keeps the old credential, which still
works during the overlap, and tries again on the next pass. It never runs on a credential that
a restart would lose.

### 4. When it happens

- **By age.** The agent rotates once its credentials reach `--credential-max-age` (chart
  `agent.credentialMaxAge`, 30 days by default). A file written before rotation existed has no
  issue time, so its age is measured from the agent's first start on the new version, which
  never rotates sooner than configured.
- **On request.** `POST /v1/accounts/{id}/stamps/{stamp_id}/credential-rotation` records the
  request (`stamps:write`). The next heartbeat reply carries `rotate_credentials: true`, the agent
  rotates, and the request clears. The response is `202 Accepted` because the work happens on the
  stamp's schedule.

### 5. The collector follows its file

The collector re-reads its telemetry credential whenever the file changes. The agent replaces the
file atomically, so a read never sees half a credential. A missing or empty file keeps the last
credential read.

## Consequences

- Rotation needs no downtime, no new enrollment token and no inbound connection.
- Each rotation is audited as `stamp.credentials_rotated`, with the version, the end of the
  overlap, and whether it was requested. Requests are audited as
  `stamp.credential_rotation_requested`.
- The stamp API reports `credential_version`, `credentials_rotated_at` and any pending request,
  and the console offers a rotation button per stamp.
- Expired credential rows are kept. They are small, they are evidence, and nothing reads them
  once expired.

## Not decided here

Rotation of the control plane's own JWT signing key, and of API keys. Both need overlap too, but
their holders and failure modes differ.
