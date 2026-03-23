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

- provider credentials come from environment variables
- provider base URLs are still subject to outbound policy checks
- each provider has its own persisted config record
- each provider can be enabled or disabled independently
- each provider can carry its own model default, auth env name, base URL, and allowed-host list
- provider requests can be routed by profile: `fast`, `cheap`, `strong`, `local-only`, `privacy-preferred`
- provider fallback is allowed only across configured candidates
- circuit breaker state is tracked, persisted in SQLite provider health rows, and surfaced in the UI

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
- the catalog shows enabled state, capabilities, auth env name, configured destinations, and which runtime profiles point at that provider
- run history and task graph views show which provider each agent role used

Data classes:

- `local-only`
  Never send to a remote-only provider.
- `external-ok`
  Allowed to leave the workstation when host policy allows it.
- `restricted`
  Blocked from remote providers unless `allow_restricted_provider_egress` is explicitly enabled.

This project does not support fake credential reuse models. Chat UI session cookies or subscription tokens are not treated as general API credentials.
