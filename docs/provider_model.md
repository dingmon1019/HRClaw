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
- provider requests can be routed by profile: `fast`, `cheap`, `strong`, `local-only`, `privacy-preferred`
- provider fallback is allowed only across configured candidates
- circuit breaker state is tracked and surfaced in the UI

Egress controls apply to providers the same way they apply to HTTP connector usage:

- allowed schemes
- allowed ports
- allowed hosts / base URLs
- redirect policy
- timeout limits
- response size limits
- private-network restrictions unless explicitly enabled

Data classes:

- `local-only`
  Never send to a remote-only provider.
- `external-ok`
  Allowed to leave the workstation when host policy allows it.
- `restricted`
  Blocked from remote providers unless `allow_restricted_provider_egress` is explicitly enabled.

This project does not support fake credential reuse models. Chat UI session cookies or subscription tokens are not treated as general API credentials.
