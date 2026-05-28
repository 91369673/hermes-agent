# LLM-Agnostic Agent Runtime Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task if Giampiero approves implementation.

**Goal:** Replace Claude-Code-specific orchestration thinking with a provider- and harness-agnostic runtime model that can use Hermes, Codex, Claude Code, Gemini, ACP-compatible agents, or future tools behind the same operational contract.

**Architecture:** Carmen and the room system should not care which LLM or coding harness executes work. They emit a normalized `AgentJobSpec`; a runtime resolver chooses the best available substrate; adapters translate the spec into each backend's command/protocol; results return as normalized `AgentRunResult` artifacts linked back to the room/blackboard.

**Tech Stack:** Hermes profiles, provider routing, `delegate_task`, cron/Kanban, ACP where available, shell/PTY adapters for external CLIs, Agent HQ blackboard, room register.

---

## Product Decision

Discard the idea: **"Use Claude Code as the special team member."**

Keep the useful idea: **"Use external high-capability agentic runtimes as interchangeable executors behind Carmen."**

The visible UX stays:

```text
Giampiero → Carmen Inbox / Meeting Room N → result + traceable artifacts
```

The implementation becomes:

```text
Room / job request
  → normalized task spec
  → capability-based runtime selection
  → adapter execution
  → verification
  → shared artifact + Carmen summary
```

No user-facing workflow should depend on whether the executor was Codex, Claude, Gemini, Hermes-native, or something else.

---

## Core Abstraction

### 1. AgentJobSpec

A durable, model-neutral job description. It describes the work, not the model.

```yaml
id: job_2026_05_28_001
room: meeting-room-2
requester: giampiero
objective: "Implement X and verify tests"
context:
  repo: /Users/giampierosirianni/.hermes/hermes-agent
  docs:
    - docs/adr/ADR-0001-carmen-primary-interface.md
  prior_artifacts: []
constraints:
  language: de
  privacy: no_secrets_in_shared_artifacts
  approval_required_for:
    - external_send
    - destructive_shell
    - credential_changes
capabilities_required:
  - read_files
  - edit_files
  - run_tests
  - git_diff
preferences:
  isolation: worktree_preferred
  duration: bounded
  max_runtime_minutes: 45
  reasoning_depth: high
acceptance:
  - "Tests pass or failures are explicitly explained"
  - "Git diff is summarized"
  - "Room register gets artifact path and next action"
```

Important: `AgentJobSpec` must avoid provider names. It says `capabilities_required`, not "use Claude".

### 2. RuntimeDescriptor

A registry entry for any possible executor.

```yaml
name: codex-cli
kind: external_cli
adapter: codex
availability_probe: "codex --version"
auth_probe: "codex auth status"
capabilities:
  - read_files
  - edit_files
  - run_tests
  - web_search_optional
supports:
  print_mode: true
  interactive_pty: true
  acp: false
isolation:
  worktree: true
  cwd: true
risk:
  can_execute_shell: true
  needs_approval_bridge: true
```

Other examples:

- `hermes-native-subagent`
- `hermes-cron-worker`
- `claude-code-cli`
- `gemini-cli`
- `copilot-acp`
- `opencode-cli`
- `manual-carmen` for tasks Carmen handles herself

### 3. RuntimeResolver

Given an `AgentJobSpec`, choose a runtime by capability and health, not by brand.

Resolution order should be configurable, for example:

```yaml
agent_runtime:
  order:
    - hermes-native-subagent
    - codex-cli
    - gemini-cli
    - claude-code-cli
  fallback_policy: next_healthy_runtime
  require_auth_probe: true
  prefer:
    local_auth: true
    worktree_isolation: true
    structured_output: true
```

Selection logic:

1. Filter runtimes by required capabilities.
2. Probe availability and auth.
3. Exclude unhealthy or unauthenticated runtimes.
4. Prefer lower-friction substrate:
   - Hermes-native for normal bounded tasks.
   - Codex/Claude/Gemini CLI for deep code work if healthy.
   - Cron/Kanban for durable or multi-hour work.
   - ACP for IDE-like editor integration when configured.
5. Record which runtime was selected and why.

### 4. RuntimeAdapter

Each backend implements the same small interface.

```python
class AgentRuntimeAdapter:
    name: str

    def probe(self) -> RuntimeHealth: ...

    def prepare(self, spec: AgentJobSpec) -> PreparedRun: ...

    def start(self, prepared: PreparedRun) -> RunHandle: ...

    def poll(self, handle: RunHandle) -> RunState: ...

    def collect(self, handle: RunHandle) -> AgentRunResult: ...

    def cancel(self, handle: RunHandle) -> None: ...
```

Adapters translate generic specs into backend-specific mechanics:

- Hermes-native: `delegate_task` or spawned `hermes chat -q`.
- Codex: CLI command / ACP if available.
- Claude Code: `claude -p` or tmux TUI if healthy.
- Gemini: CLI or API-backed Hermes provider.
- Cron/Kanban: durable job/task creation.

### 5. AgentRunResult

A normalized output artifact.

```yaml
job_id: job_2026_05_28_001
runtime: codex-cli
status: completed
started_at: 2026-05-28T14:30:00+02:00
finished_at: 2026-05-28T14:48:00+02:00
artifacts:
  summary: /shared/agent-hq/runs/job_.../summary.md
  diff: /shared/agent-hq/runs/job_.../diff.patch
  logs: /shared/agent-hq/runs/job_.../run.log
verification:
  commands:
    - command: "python -m pytest tests/foo -q"
      status: passed
next_actions:
  - "Ask Giampiero whether to merge"
```

Carmen consumes this and reports in human language:

```text
Ich habe Meeting Room 2 auf Codex-CLI gelegt, weil Claude gerade 401 liefert und Codex gesund ist. Ergebnis: Tests grün, Diff liegt hier, nächster Schritt ist Review/Merge.
```

---

## Where This Fits in Company OS

### Carmen remains primary interface

Carmen should never say: "Go talk to Claude/Codex/Gemini."

She should say:

```text
Ich lege das in Room 3. Executor: beste verfügbare Coding Runtime. Ich melde mich, wenn Tests/Diff da sind.
```

The executor is implementation detail, visible only as metadata.

### Rooms hold human context

`ROOMS.yaml` should track:

```yaml
meeting-room-3:
  title: "Hermes provider-agnostic runtime"
  state: active
  owner: carmen
  executor:
    kind: agent_runtime
    selected: codex-cli
    fallback: hermes-native-subagent
  artifacts:
    - docs/plans/2026-05-28-llm-agnostic-agent-runtime.md
  next_update: "after implementation plan review"
```

### Agent HQ stores operational traces

Agent HQ should store:

```text
runs/<job_id>/spec.yaml
runs/<job_id>/runtime.yaml
runs/<job_id>/summary.md
runs/<job_id>/verification.md
runs/<job_id>/diff.patch
```

No secrets, no raw private memory, no copied auth tokens.

---

## Implementation Plan

### Task 1: Add architecture ADR

**Objective:** Record the decision to use provider-agnostic runtimes.

**Files:**
- Create: `docs/adr/ADR-llm-agnostic-agent-runtime.md`

**Content:**
- Context: Claude Code auth failed; depending on one external tool is brittle.
- Decision: rooms emit `AgentJobSpec`; runtime resolver selects healthy backend.
- Consequences: more adapter work, but less vendor lock-in.
- Status: proposed.

**Verification:** ADR exists and does not include secrets or provider-specific credentials.

### Task 2: Define data contracts

**Objective:** Create typed models for specs/results without wiring execution yet.

**Files:**
- Create: `agent/runtime_contracts.py`
- Test: `tests/agent/test_runtime_contracts.py`

**Models:**
- `AgentJobSpec`
- `RuntimeDescriptor`
- `RuntimeHealth`
- `PreparedRun`
- `RunHandle`
- `RunState`
- `AgentRunResult`

**Verification:** Unit tests serialize/deserialize all contracts to JSON/YAML-safe dicts.

### Task 3: Add runtime registry

**Objective:** Register available runtimes declaratively.

**Files:**
- Create: `agent/runtime_registry.py`
- Test: `tests/agent/test_runtime_registry.py`

**Initial runtimes:**
- `hermes-native-subagent`
- `hermes-spawned-cli`
- `external-cli-placeholder`

**Verification:** Registry can list descriptors and filter by capability.

### Task 4: Add resolver

**Objective:** Select a runtime based on capability, health, and configured preference.

**Files:**
- Create: `agent/runtime_resolver.py`
- Test: `tests/agent/test_runtime_resolver.py`

**Rules:**
- Required capabilities are hard filters.
- Unhealthy/auth-failed runtimes are skipped.
- Resolver returns selected runtime plus explanation.
- If none match, returns actionable failure.

**Verification:** Tests cover healthy first choice, fallback, no-capability match, and auth failure.

### Task 5: Add Hermes-native adapter first

**Objective:** Prove the abstraction without depending on external LLM CLIs.

**Files:**
- Create: `agent/runtime_adapters/hermes_native.py`
- Test: `tests/agent/runtime_adapters/test_hermes_native.py`

**Behavior:**
- Converts `AgentJobSpec` to a `delegate_task` or spawned `hermes chat -q` execution plan.
- Does not create recursive cron jobs.
- Respects enabled toolsets and workdir.

**Verification:** Dry-run mode produces the expected prompt and runtime metadata.

### Task 6: Add external CLI adapter shape

**Objective:** Support Codex/Claude/Gemini without naming them in room logic.

**Files:**
- Create: `agent/runtime_adapters/external_cli.py`
- Test: `tests/agent/runtime_adapters/test_external_cli.py`

**Behavior:**
- Supports `probe_command`, `auth_probe_command`, `run_command_template`.
- Captures stdout/stderr/log path.
- Requires explicit capability declaration.
- Does not read secrets from output into shared artifacts.

**Verification:** Tests use fake shell scripts, not real Codex/Claude/Gemini.

### Task 7: Wire a room-facing command later

**Objective:** Let Carmen create jobs from rooms without exposing provider choice to Giampiero.

**Files:** TBD after reviewing existing room/router implementation.

**Potential UX:**

```text
/room run 3 "Implement the runtime resolver and tests"
```

or natural language:

```text
Carmen, leg das in Raum 3 und nimm den besten Coding-Executor.
```

**Verification:** Room register records selected executor and artifacts.

---

## Non-Goals

- Do not make Claude Code a privileged dependency.
- Do not copy Claude/Codex/Gemini auth tokens into Hermes memory or shared files.
- Do not create a new visible bot per LLM.
- Do not make Giampiero choose models for routine work.
- Do not solve every external CLI's quirks in the first pass.

---

## Recommended First Slice

Build only the contracts, registry, resolver, and Hermes-native dry-run adapter first.

That gives us the architecture without betting on any external CLI. Once that is stable, Codex/Claude/Gemini are just adapters.

The key rule:

```text
Rooms route work. Runtimes execute work. LLM vendors are replaceable components.
```
