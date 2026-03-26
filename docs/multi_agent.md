# Multi-Agent Model

## Purpose

The project now uses real multi-agent orchestration rather than naming a single planner as multiple things.

Each shipped role has:

- a stable identity
- an explicit role
- a provider profile
- a capability set
- an allowed connector set
- a memory namespace
- an agent-scoped scratch work area under the runtime state root
- persisted run history

## Roles

### Supervisor Agent

- role: `supervisor`
- primary job: interpret the operator objective
- output: subtask decomposition and handoffs

### Planner Agent

- role: `planner`
- primary job: turn subtasks into typed candidate actions
- output: candidate proposals and planning summary

### Reviewer Agent

- role: `reviewer`
- primary job: apply risk, policy, and egress review
- output: approval-ready proposals or blocked proposals

### Executor Agent

- role: `executor`
- primary job: execute approved snapshots only
- output: execution results or failure records

### Reporter Agent

- role: `reporter`
- primary job: explain plans and outcomes to the operator
- output: workbench summary text

## Persistence Model

The following records make the multi-agent model visible:

- `agents`
  Role definitions and capabilities.
- `agent_runs`
  One row per agent step in a run.
- `handoffs`
  Explicit transfers between roles.
- `task_nodes`
  Parent/child and dependency-linked orchestration nodes for the run graph.
- `proposals`
  Proposal provenance back to planner and reviewer roles.

## Current Orchestration Pattern

The graph is now the primary lifecycle engine from run admission onward:

1. the run is registered in `graph_runs` as soon as the operator objective is accepted
2. a supervisor objective node performs durable decomposition and creates the downstream planning scaffold
3. a planning-summary node collects descriptor-only context, generates the planning summary, and persists it durably
4. planner branch nodes create candidate actions once the summary node completes
5. reviewer nodes are created per branch, so risk and egress checks are visible on each branch instead of only once globally
6. a merge node waits for the reviewed branches before reporter synthesis proceeds
7. proposal nodes are attached under the relevant reviewed branch with explicit proposal IDs
8. executor nodes are created per proposal and sit in `waiting_approval`, `queued`, `running`, `completed`, or `failed` states
9. executor work later runs inside the worker child-process boundary after approval

The persisted graph is now reconciled against durable runtime state:

- proposal approval moves the related executor node into `queued`
- worker claim moves the executor node into `running`
- worker success moves the node into `executed`
- rejection, cancellation, or stale detection moves the node into the matching terminal state
- startup reconciliation can recover expired running jobs and re-mark executor nodes after a restart
- merge nodes remain blocked until their reviewed branch dependencies are complete

Persisted task nodes track:

- node type
- owner agent role
- provider profile
- context namespace
- parent edge
- dependency edges
- lifecycle state
- reasoning summary
- agent work-area metadata (shared workspace root, scratch root, promotion root)
- proposal-scoped executor scratch areas so same-connector proposals in one run do not silently share writable scratch state
- artifact-lineage records for work-area assignment and explicit promotion-style filesystem transfers

This is a practical local-first orchestration layer. It is not a distributed agent mesh.

Execution mode is explicit:

- `inline_compat`
  The graph is admitted first, then the caller drains non-executor graph jobs inline for compatibility.
- `background_preferred`
  The graph is admitted and queued immediately. Startup/restart reconciliation restores graph state but does not silently execute provider-backed graph work inline.
- `background_only`
  Same admission model as `background_preferred`, intended for worker-driven orchestration only.

## Current Limits

- agent memory is namespaced by role and branch, but not yet a full long-term memory subsystem
- agent prompts are mostly role-specific heuristics, not full autonomous loops
- the scheduler is a bounded in-process graph scheduler, not a distributed orchestration mesh
- executor work is still approval-gated and worker-bound, not a fully parallel branch executor
- there is no per-user RBAC on agent capabilities yet

## Why This Still Matters

Even with a bounded sequential runtime, explicit multi-agent persistence gives operators answers to practical questions:

- who proposed this action?
- who reviewed it?
- what handoff led to this step?
- what task node dependencies led to this step?
- which provider profile was used for this role?
- what changed between planning and execution?
- which files are still agent-local scratch data and what would need explicit promotion into the shared workspace?
