# Threat Model

## Primary Threats Considered

This project is a localhost runtime, but localhost still has a threat surface.

The most important threats considered here are:

- another local user or process hitting dangerous routes without auth
- CSRF or browser-origin confusion on localhost
- silent drift between approved content and executed content
- unrestricted filesystem writes into the repository or runtime state
- raw shell execution escaping the intended tool model
- provider adapters bypassing HTTP policy
- sensitive prompts leaving the machine through a remote provider
- audit tampering after the fact
- worker crashes leaving jobs stuck or ambiguous

## Out Of Scope Threats

These are not solved by this repository alone:

- kernel compromise
- malware already running with high privileges
- memory scraping on the host
- hardware-backed secret extraction
- full endpoint isolation

## Trust Assumptions

The current system assumes:

- the host OS is not already fully compromised
- the operator controls the machine
- the loopback-bound web app is not exposed beyond localhost
- the chosen providers are trusted according to the operator's policy

## Current Mitigations

- login/logout and session timeout
- recent re-authentication for sensitive actions
- CSRF on POST routes
- loopback-oriented host/origin validation
- approval snapshot hashes
- bounded connector schemas
- no raw shell execution
- workspace allowlist and protected write blocking
- provider host allowlists and restricted-data refusal
- worker lease and attempt tracking
- hash-chained audit trail

## Future Hardening Roadmap

The next meaningful security steps would be:

- stronger Windows secret storage
- worker execution under reduced privileges
- Windows service mode with least privilege
- signed audit export bundles
- richer RBAC
- stronger isolation around filesystem and network side effects
