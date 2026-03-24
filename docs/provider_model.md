## Provider Model

This runtime supports multiple provider adapters, but provider flexibility is constrained by policy.

Supported providers:

- `mock`
- `openai`
- `openai-compatible`
- `generic-http`
- `anthropic`
- `gemini`

Core rules:

- provider credentials come from environment variables or optional Windows Credential Manager targets
- provider base URLs are still subject to outbound policy checks
- each provider has its own persisted config record
- each provider can be enabled or disabled independently
- each provider can carry its own model default, auth source, credential target, base URL, allowed-host list, and routing posture
- provider requests can be routed by profile: `fast`, `cheap`, `strong`, `local-only`, `privacy-preferred`
- provider fallback is allowed only across configured candidates
- circuit breaker state is tracked, persisted in SQLite provider health rows, and surfaced in the UI
- provider scoring considers capability fit, privacy, cost, latency, and recent success/failure history
- provider routing considers every enabled configured provider that satisfies policy and capability requirements, not only a narrow fallback chain
- remote provider requests use curated outbound-safe prompt variants when local-only data was collected during planning or reporting

Egress controls apply to providers the same way they apply to HTTP connector usage:

- allowed schemes
- allowed ports
- allowed hosts / base URLs
- redirect policy
- timeout limits
- response size limits
- private-network restrictions unless explicitly enabled
- provider health records expose allowed hosts, profiles, circuit state, and per-provider config status in the operator console

Operator visibility:

- the Settings page exposes a provider catalog instead of a single global provider form
- the catalog shows enabled state, capabilities, auth source, configured destinations, routing posture, reliability counters, and which runtime profiles point at that provider
- run history and task graph views show which provider each agent role used
- routing decisions are emitted into audit/log flows so operators can see why a provider was selected
- observed latency EWMA, success rate, failure streak, and last error category are shown in the provider catalog

Data classes:

- `local-only`
  Never send to a remote-only provider.
- `external-ok`
  Allowed to leave the workstation when host policy allows it.
- `restricted`
  Blocked from remote providers unless `allow_restricted_provider_egress` is explicitly enabled.

Routing inputs include:

- agent role
- task type
- routing profile
- data classification
- operator override
- capability fit
- privacy posture
- cost tier
- observed latency
- recent failure history

This project does not support fake credential reuse models. Chat UI session cookies or subscription tokens are not treated as general API credentials.
