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

Today the orchestration is bounded, explicit, and graph-shaped rather than a flat step log:

1. supervisor run starts and writes the objective root node
2. planner subtask nodes are created as child branches under the objective
3. reviewer nodes depend on planner branches and block until planning completes
4. reporter nodes depend on reviewer completion
5. proposal nodes are attached under the relevant planner branch with reviewer dependencies
6. executor runs later inside the worker after approval

This is a practical local-first orchestration layer. It is not a distributed agent mesh.

## Current Limits

- agent memory is namespaced by role, but not yet a full long-term memory subsystem
- agent prompts are mostly role-specific heuristics, not full autonomous loops
- planner branches can be represented as parallel-ready nodes, but there is no general parallel worker scheduler yet
- there is no per-user RBAC on agent capabilities yet

## Why This Still Matters

Even with a bounded sequential runtime, explicit multi-agent persistence gives operators answers to practical questions:

- who proposed this action?
- who reviewed it?
- what handoff led to this step?
- what task node dependencies led to this step?
- which provider profile was used for this role?
- what changed between planning and execution?
