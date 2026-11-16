# Attenuable capability tokens

Capabilities are optional. Existing opaque visas, administrator credentials and
mapped agent credentials remain supported; the hosted sandbox does not enable
capabilities. A human administrator mints a capability for an existing ordinary
visa. A holder can append a narrower child contract offline, without the signing
key or a gateway call. The resulting bearer token works through the same `/execute`
and MCP tools. It cannot approve requests, mint roots or access administrator data.

## Construction and boundaries

The versioned `nodi1` format uses the first-party
[chained-HMAC construction described in the macaroon paper](https://research.google/pubs/macaroons-cookies-with-contextual-caveats-for-decentralized-authorization-in-the-cloud/).
A domain-separated HMAC-SHA256 authenticates the root identifier, key ID, source
visa, ledger epoch and complete contract. Each appended caveat contains a nonce and
complete narrower contract; its HMAC uses the preceding signature as the key.
Only the final signature is transmitted. Verifiers recompute the entire chain and
compare signatures in constant time. Holders know the current signature, so they
can append caveats but cannot calculate a removed prefix's signature or rewrite an
earlier caveat. Every caveat also passes the existing `policy.attenuation()` check;
a valid MAC alone never legitimizes wider authority.

Each child preserves mission and domain, reduces delegation depth, narrows allowed
sets, retains forbidden sets and cannot raise budget, transaction amount, approval
threshold, action count or expiry. Agent identity may change to a delegated label.
Depth always decreases, even if every other bound is retained. Claims are finite,
unambiguous canonical JSON, bounded to eight caveats and a 32 KiB encoded token.
Unknown fields, malformed contracts, duplicate keys, noncanonical encodings and
unsupported versions fail closed. Infrastructure may impose smaller header limits.

This is a small custom first-party format, **not** macaroon/Biscuit wire compatibility,
a Biscuit Datalog implementation, third-party discharges, public-key signatures,
authenticated workload identity, or independently audited cryptography. It relies on
HMAC-SHA256, collision resistance of SHA-256 scope IDs, secret uniformly random
32-byte keys, trusted gateway code/storage and correct caveat verification. External
review is required before security-critical deployment. Tokens are not encrypted.

These are bearer credentials. Anyone possessing one can exercise its scope and
further attenuate it. A holder can retain and use its stronger parent credential;
creating a child does not erase or revoke that parent. Giving away a token is not
proof of the recipient's identity. Possession is the authentication mechanism;
`agent_id` is a delegated label, not verified human or workload identity.

## Why the ledger remains authoritative

The signed root is registered in the issuing database and bound to a source-visa
epoch. Using the same signing key with a different database does not create authority.
Every execution checks the current root registration, epoch, source contract,
ancestor status/expiry and depth under `BEGIN IMMEDIATE`. On first use, verified
prefixes become immutable visa rows under their source. Deterministic prefix IDs
mean a token copy or retry uses the same rows. Siblings share their prefix rows and
all source ancestors; neither offline derivation nor root reissuance creates extra
money. No funds are reserved merely by delegating. Per-action outbox reservations
continue to use every ledger ancestor.

Materialization, request-to-root binding, policy/intent evaluation, simulated effect,
budget update and audit commit together. All existing hard checks still run. Slow
evaluation gets the existing final expiry check. A token-bound request cannot be
rebound to another capability root or retried with an ordinary agent credential;
approval remains bound to the exact action and request fingerprint. Cryptographic
and stale-capability failures return HTTP 401 without an effect; authenticated
policy denials still return a normal DENY decision. Authorization failures before
the engine are not audit decision events. Tokens/signatures/keys are never stored
in audit records, although public claims and contracts are retained in the ledger.

Capability prefixes cannot be edited or delegated through the legacy API. Derive
another token for a changed scope, or revoke the stored prefix to stop its entire
subtree. Before a prefix has been materialized, revoke its source or an already
materialized ancestor; per-token pre-registration revocation is not implemented.
Editing the source visa increments its epoch and invalidates all roots previously
minted from it, even if the old contract is later restored. Normal ancestor edits
still apply immediately through the existing chain checks. Revocation/expiry is
checked even before returning a token-authenticated replay or status response.
An administrator can still inspect historical results through the control routes.

Queued payments retain their token-root binding. Source edits invalidate future
pending dispatch/absent retries; revoked or expired ancestors cannot authorize new
dispatch. An already committed external effect is still reconciled and accounted
for after revocation, and uncertain reservations stay held. Retiring a signing key
blocks new token presentations; it does not retroactively cancel accepted outbox
work. Revoke the source/prefix to stop new dispatch. Workers use stored ledger
bindings, never bearer tokens or signing keys. The controlled-provider assumptions
in [outbox recovery](outbox.md) continue to apply.

Plain self-contained bearer JWTs do not provide holder-appended, verifiable
attenuation. Neither JWTs nor these capabilities independently solve revocation,
shared budget consumption, approval replay or outbox recovery. A JWT design could
also consult a ledger, but would ordinarily require a trusted issuer to mint each
narrower token. This hybrid trades offline delegation for online execution checks,
stateful scope registration, a trusted symmetric-key issuer and larger credentials.
It does not provide stateless execution or public verifiability.

## Enable locally

Generate a random key with `secrets.token_hex(32)` on the operator's machine and
store it in the private `.nodi.local.json` configuration as:

```json
{"capability_keys": {"v1": "<64 lowercase hex characters from a random 32-byte key>"}}
```

Keep the existing configuration fields and owner-only permissions. The placeholder
is not a usable key. `python -m nodi serve` loads this optional field. The environment
entry point `nodi.api:from_environment` accepts the equivalent `NODI_CAPABILITY_KEYS`
JSON map. No key is auto-created, embedded in source, or enabled on the public demo.
Restart with both old/new IDs during rotation; issue new roots with the new ID,
then remove the old ID to reject old tokens. Keep keys out of agent environments.

Using the human administrator credential, POST to `/visas/{visa_id}/capability`
with `{"key_id":"v1"}`. The no-store response contains `token`, `visa_id`, `root_id`
and the complete contract. Save only the token into a private `parent.token` file;
do not put it in shell history, URLs, public reports or source control.

Prepare a complete child contract JSON, lowering `delegation_depth` and any desired
limits, then run offline:

```sh
python -m nodi.capabilities attenuate --input parent.token \
  --contract child-contract.json --output child.token
python -m nodi.capabilities describe --input child.token
```

The output token file is created with mode 0600 and never overwrites an existing
file. The command prints only its path and public visa ID. `describe` explicitly
reports `signature_verified: false`: an offline holder cannot validate the issuer's
root signature. Gateway verification remains mandatory.

Use the child token as `Authorization: Bearer ...` on `/execute`, with the child
`visa_id` returned by `describe`. The token's principal is derived from verified
claims, not a request identity field. `/requests/status` permits only this exact
scope/root's results. For MCP, supply the child token via `NODI_AGENT_TOKEN` and use
the same two existing tools. Do not give the agent an administrator credential or
signing key. Capability tokens intentionally cannot call the legacy delegation
endpoint: derive narrower credentials offline instead.

## Reproducible validation

```sh
python -m nodi.capability_demo
HYPOTHESIS_PROFILE=nodi-ci python -m pytest -q tests/test_capabilities.py \
  tests/test_capability_invariants.py
```

The demo reports actual local simulated decisions, shared spend and revocation.
Unit/integration checks cover forgery, stripping/reordering caveats, valid-MAC scope
widening, cross-ledger use, key retirement, HTTP/MCP boundaries, exact approvals,
source edit-and-restore, outbox uncertainty, rollback, and 10/50 concurrent siblings.
Generated histories extend the existing invariants to offline trees up to depth
four and 30 tokens, semantic outcomes, approvals, replay, expiry and revocation.
These are bounded regression tests, not a cryptographic proof or measured attack
success probability. No real payment, email, model call or deployment is required.
