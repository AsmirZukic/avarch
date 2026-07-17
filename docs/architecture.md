# Avarch Architectural Design Philosophy

**Status:** Adopted  
**Adopted:** 2026-06-24  
**Applies to:** All new code and the architectural refactor of the existing Avarch codebase  
**Primary audience:** Contributors and coding agents implementing or reviewing structural changes

## 1. Purpose

This document defines the architectural philosophy Avarch will follow as the codebase grows
beyond its original intentionally flat structure.

It is an architectural contract. Refactors and new features should follow these rules unless a
later architecture decision explicitly replaces them.

The goal is not to make Avarch look academically perfect. The goal is to make it:

- easier to understand;
- easier to test;
- safer to change;
- smaller where possible;
- resistant to state-management and filesystem bugs;
- explicit about workflow and failure behavior;
- practical for a local-first command-line application.

Avarch must remain a focused media archival tool, not evolve into a generic workflow framework.

## 2. Architectural Identity

Avarch is:

> A local-first, persisted media workflow engine with a functional decision core and an
> imperative execution shell.

The primary architectural style is:

1. Functional Core, Imperative Shell
2. Explicit persisted state machine
3. Lightweight ports and adapters
4. Workflow-oriented modules
5. Functions by default; classes only where state or lifecycle justifies them

These ideas reinforce each other:

- The functional core contains deterministic decisions and business rules.
- The imperative shell gathers facts, performs I/O, invokes tools, and persists outcomes.
- The state machine defines legal job lifecycle transitions.
- Ports isolate genuinely external systems.
- Workflow modules make application behavior easy to locate.
- Restraint prevents architecture from becoming larger than the application.

## 3. Core Workflow Model

Avarch should be designed around its durable workflow, not around its CLI commands, database
tables, or external tools.

The high-level workflow is:

```text
discover
  -> inspect
  -> plan
  -> queue
  -> encode
  -> validate
  -> promote
  -> clean up
```

Recovery, cancellation, retry, and skipping are part of this workflow rather than exceptional side
features.

The central domain concepts include:

- media file identity;
- probe information and freshness;
- encoding profile;
- deterministic encoding plan;
- job and attempt;
- job state transition;
- candidate output;
- validation facts and validation report;
- promotion decision;
- failure, skip, cancellation, and recovery reason.

External tools are implementation details:

- Av1an performs video encoding;
- FFmpeg performs muxing and media operations;
- ffprobe gathers media facts;
- SQLite persists workflow state;
- the filesystem stores originals, candidates, and promoted outputs.

The domain must not be structured around those tools.

## 4. Functional Core, Imperative Shell

### 4.1 Functional Core

The functional core contains code that decides what Avarch should do.

It should primarily consist of:

- immutable values;
- enums;
- pure functions;
- deterministic policies;
- state transition logic;
- validation rules;
- planning rules;
- recovery decisions;
- resource classification;
- retry and skip decisions.

A core function:

- receives all required information through parameters;
- returns a value describing its result;
- does not access the filesystem;
- does not query SQLite;
- does not invoke subprocesses;
- does not read environment variables;
- does not read the system clock directly;
- does not log as part of its behavior;
- produces the same output for the same input.

Example:

```python
@dataclass(frozen=True)
class ValidationFacts:
    candidate_exists: bool
    probe_readable: bool
    expected_container: bool
    expected_video_codec: bool
    source_size_bytes: int
    candidate_size_bytes: int
    duration_delta_seconds: float


@dataclass(frozen=True)
class ValidationPolicy:
    require_smaller_output: bool
    maximum_duration_delta_seconds: float


def evaluate_candidate(
    facts: ValidationFacts,
    policy: ValidationPolicy,
) -> ValidationReport:
    if not facts.candidate_exists:
        return ValidationReport.failed(
            ValidationFailure.CANDIDATE_MISSING
        )

    if not facts.probe_readable:
        return ValidationReport.failed(
            ValidationFailure.CANDIDATE_UNREADABLE
        )

    if not facts.expected_container:
        return ValidationReport.failed(
            ValidationFailure.WRONG_CONTAINER
        )

    if not facts.expected_video_codec:
        return ValidationReport.failed(
            ValidationFailure.WRONG_VIDEO_CODEC
        )

    if facts.duration_delta_seconds > policy.maximum_duration_delta_seconds:
        return ValidationReport.failed(
            ValidationFailure.DURATION_MISMATCH
        )

    if (
        policy.require_smaller_output
        and facts.candidate_size_bytes >= facts.source_size_bytes
    ):
        return ValidationReport.skipped(
            SkipReason.OUTPUT_NOT_SMALLER
        )

    return ValidationReport.accepted()
```

This function does not know how the facts were collected and does not perform any cleanup or
promotion.

### 4.2 Imperative Shell

The imperative shell performs effects and coordinates the workflow.

Its responsibilities include:

- reading configuration;
- opening database sessions;
- querying and updating persisted state;
- reading file metadata;
- invoking Av1an, FFmpeg, and ffprobe;
- handling processes and signals;
- acquiring locks;
- collecting validation facts;
- calling pure decision functions;
- applying returned decisions;
- deleting rejected candidates;
- promoting accepted candidates;
- producing logs and CLI output.

Example:

```python
facts = validation_probe.collect(source_path, candidate_path)
report = evaluate_candidate(facts, policy)
job_store.record_validation(job_id, report)

if report.is_accepted:
    promotion_workflow.run(job_id)
elif report.should_discard_candidate:
    candidate_files.delete_if_present(candidate_path)
```

The rule is:

> The shell discovers what happened. The core decides what it means.

The shell should contain minimal business logic. Branches in shell code should generally apply
decisions already made by the core.

## 5. Explicit Persisted State Machine

Job state must be modeled explicitly and centrally.

No module may freely assign arbitrary job status values.

Bad:

```python
job.status = "validating"
session.commit()
```

Preferred:

```python
transition = transition_job(
    snapshot=job_snapshot,
    event=ValidationStarted(),
)
job_store.apply_transition(job_id, expected_version, transition)
```

### 5.1 State Model

The exact names may evolve, but the lifecycle must represent independently schedulable stages.

A representative lifecycle is:

```text
PLANNED
  -> QUEUED
  -> ENCODING
  -> AWAITING_VALIDATION
  -> VALIDATING
  -> AWAITING_PROMOTION
  -> PROMOTING
  -> COMPLETED
```

Terminal or alternate states include:

```text
SKIPPED
FAILED
CANCELLED
INTERRUPTED
```

The model must distinguish:

- a job waiting to begin encoding;
- an encoding process currently running;
- an encode that finished and is ready for validation;
- validation currently running;
- validation that succeeded and is ready for promotion;
- promotion currently running;
- a completed workflow;
- a normal skip outcome;
- an execution failure;
- cancellation requested versus cancellation completed;
- an interrupted stage requiring recovery.

### 5.2 Transition Rules

All legal transitions must be defined in one domain location.

A transition function should:

- validate that the event is legal for the current state;
- return the new state;
- return relevant domain outcomes;
- describe follow-up effects when useful;
- reject impossible transitions;
- never perform I/O.

Example shape:

```python
def transition_job(
    snapshot: JobSnapshot,
    event: JobEvent,
) -> JobTransition:
    ...
```

The persistence layer must apply transitions atomically.

Where concurrent workers are possible, transition writes should use an expected version,
compare-and-swap, guarded update, or equivalent mechanism to prevent stale workers from
overwriting newer state.

### 5.3 Scheduler Implications

Encoding, validation, and promotion are separate stages.

The scheduler may therefore:

- keep a heavy encoding slot occupied by one job;
- validate a completed candidate while another job encodes;
- promote an accepted candidate independently;
- apply separate concurrency limits by resource class.

The scheduler coordinates work. It must not contain the validation, promotion, or planning rules
themselves.

## 6. Lightweight Ports and Adapters

Avarch should isolate systems that are external, effectful, slow, nondeterministic, or difficult to
test.

Likely ports include:

- job persistence;
- media/probe persistence;
- process execution;
- Av1an execution;
- FFmpeg execution;
- ffprobe execution;
- filesystem operations;
- process supervision;
- locking;
- clock access.

Ports should be narrow and expressed in Avarch's language.

Preferred:

```python
class JobStore(Protocol):
    def claim_next_encode(self) -> JobSnapshot | None:
        ...

    def claim_next_validation(self) -> JobSnapshot | None:
        ...

    def apply_transition(
        self,
        job_id: JobId,
        expected_version: int,
        transition: JobTransition,
    ) -> None:
        ...
```

Avoid generic CRUD abstractions:

```python
class Repository:
    def create(self, value): ...
    def read(self, identifier): ...
    def update(self, value): ...
    def delete(self, identifier): ...
```

Generic repositories expose storage mechanics instead of domain intent.

### 6.1 When an Abstraction Is Justified

Add a port or interface when at least one of these is true:

- it crosses an architectural boundary;
- it isolates I/O;
- it enables deterministic testing;
- multiple implementations genuinely exist;
- the behavior has an independent lifecycle;
- it protects important domain terminology or invariants.

Do not create interfaces merely because a class exists.

Do not create a fake for every object. Prefer simple test doubles at actual external boundaries.

## 7. Package and Folder Structure

The structure should remain shallow enough to navigate quickly while separating responsibilities
clearly.

Recommended target:

```text
src/avarch/
|-- domain/
|   |-- media.py
|   |-- profiles.py
|   |-- planning.py
|   |-- jobs.py
|   |-- validation.py
|   |-- promotion.py
|   `-- recovery.py
|
|-- application/
|   |-- database_admin.py
|   |-- enqueue.py
|   |-- file_views.py
|   |-- inventory_scan.py
|   |-- job_control.py
|   |-- job_views.py
|   |-- manual_validation.py
|   |-- plan_artifacts.py
|   |-- plan_views.py
|   |-- planning.py
|   |-- planning_workflow.py
|   |-- probing.py
|   |-- profile_management.py
|   |-- promotion.py
|   |-- queue_control.py
|   |-- scheduler_*.py
|   |-- validation_summary.py
|   |-- vapoursynth_*.py
|   |-- workflow_run.py
|   |-- workflow_wait.py
|   `-- workspace_management.py
|
|-- adapters/
|   |-- sqlite/
|   |   |-- models.py
|   |   |-- admin.py
|   |   |-- enqueue.py
|   |   |-- file_views.py
|   |   |-- job_*.py
|   |   |-- manual_validation.py
|   |   |-- plan_views.py
|   |   |-- planning.py
|   |   |-- probing.py
|   |   |-- queue_control.py
|   |   |-- scheduler_*.py
|   |   |-- validations.py
|   |   `-- migrations.py
|   |-- execution.py
|   |-- inventory_scan.py
|   |-- job_preparation.py
|   |-- manual_validation.py
|   |-- probe.py
|   |-- promotion*.py
|   |-- scheduler_*.py
|   |-- validation.py
|   |-- vapoursynth.py
|   |-- vpy_env.py
|   `-- vpy_plugins.py
|
|-- cli.py
|-- config.py
`-- bootstrap.py
```

The exact module names may continue to evolve, but this is the current intended shape.

Do not create empty packages, speculative modules, or one file per trivial class.

Related domain values and policies may share a module until the module has a clear reason to split.

### 7.1 Dependency Direction

Allowed dependency direction:

```text
CLI / scheduler
       |
       v
application workflows
       |
       v
domain

adapters implement ports required by application workflows
bootstrap composes concrete implementations
```

Rules:

- `domain` imports only the Python standard library and deliberately approved type-only utilities.
- `domain` must not import SQLModel, Typer, subprocess, Av1an, FFmpeg, ffprobe, CLI modules,
  scheduler modules, or adapters.
- `application` may import `domain` and port definitions.
- `application` must not depend directly on concrete SQLite or subprocess implementations.
- `adapters` may import domain and application port types.
- `cli` may call application workflows but must not implement domain policy.
- `scheduler` may call application workflows but must not bypass them to mutate domain state.
- `bootstrap.py` is the composition root where concrete adapters are assembled.

Circular imports are architectural defects and must not be solved with local imports unless the cycle
is temporary and explicitly documented during a staged refactor.

## 8. Workflow Modules

Application behavior should be organized by meaningful workflow:

- scan;
- probe;
- plan;
- enqueue;
- encode;
- validate;
- promote;
- recover;
- cancel;
- clear queue.

A workflow module coordinates a use case.

It may:

- load required state through ports;
- collect facts;
- invoke domain decisions;
- call adapters;
- persist transitions;
- return an application-level result.

It should not:

- contain unrelated use cases;
- become a global `Service` class;
- hide multiple stages in one giant method;
- implement detailed CLI rendering;
- duplicate validation or transition policy;
- expose SQLModel entities as its public API.

Prefer a function when the workflow has no durable in-memory state:

```python
def validate_job(
    command: ValidateJob,
    dependencies: ValidationDependencies,
) -> ValidateJobResult:
    ...
```

A small class is acceptable where dependency grouping or lifecycle makes it clearer:

```python
class ProcessSupervisor:
    ...
```

## 9. Functions First, Classes Where Justified

Functions are the default for:

- calculations;
- parsing;
- validation;
- transformation;
- state transitions;
- policy evaluation;
- command construction;
- deterministic planning;
- workflow orchestration without retained state.

Classes are appropriate for:

- database-backed adapters;
- long-lived subprocess supervision;
- worker lifecycle;
- resources that must be opened and closed;
- objects protecting real invariants;
- adapters implementing protocols;
- stateful coordination.

Avoid classes whose only purpose is namespacing.

Avoid vague types such as:

- `Manager`;
- `Helper`;
- `Utils`;
- `CommonService`;
- `Handler`;
- `Processor`.

Use names from the domain:

- `JobStore`;
- `ProcessSupervisor`;
- `OutputValidator`;
- `PromotionTransaction`;
- `ProfileResolver`;
- `CandidateFiles`;
- `EncodingWorker`.

`common.py` is allowed only for genuinely shared entry-point helpers. It must not become a dumping
ground for unrelated application logic.

## 10. Domain Values and Persistence Models

The persisted SQLModel representation is not the domain model.

Mutable database rows should remain inside the SQLite adapter wherever practical.

The core should use explicit values such as:

```python
@dataclass(frozen=True)
class FileIdentity:
    canonical_path: Path
    size_bytes: int
    modified_time_ns: int
```

```python
@dataclass(frozen=True)
class EncodingPlan:
    source: FileIdentity
    profile_revision: ProfileRevision
    video: VideoPlan
    audio: AudioPlan
    container: ContainerPlan
```

Benefits:

- domain rules do not accidentally perform persistence work;
- tests do not require database entities;
- mutation becomes deliberate;
- invalid combinations can be rejected at construction;
- database schema changes do not automatically spread through the entire codebase.

Do not duplicate every database table as a domain object. Add domain values when they express
behavior, invariants, or terminology.

Mapping code should remain small and close to the adapter boundary.

## 11. Error and Outcome Model

Expected business outcomes must be returned as values.

Examples:

- candidate output is not smaller;
- media file is no longer eligible;
- plan is stale;
- candidate failed validation;
- existing output already satisfies the plan;
- job was cancelled;
- job should be skipped;
- retry limit was reached.

Example:

```python
ValidationReport.skipped(
    SkipReason.OUTPUT_NOT_SMALLER
)
```

Execution failures should be represented separately.

Examples:

- ffprobe could not be launched;
- Av1an exited unexpectedly;
- SQLite commit failed;
- permission was denied;
- the candidate disappeared during validation;
- promotion could not be completed safely.

Example categories:

```text
ProcessLaunchError
EncodingExecutionError
ValidationExecutionError
PersistenceError
PromotionExecutionError
FilesystemOperationError
```

Rules:

- Do not use exceptions for routine skip decisions.
- Do not reduce distinct failures to a generic `False`.
- Do not catch broad exceptions and silently continue.
- Translate low-level exceptions at architectural boundaries.
- Persist enough structured information to diagnose failures.
- Preserve the original exception as the cause where appropriate.
- User-facing messages must be derived from structured outcomes, not used as the stored source of
  truth.

## 12. Idempotency and Recovery

Every durable workflow stage must be designed to tolerate retries and process restarts.

For every operation, answer:

> What happens if this operation runs twice?

Required behavior includes:

- scanning an unchanged file does not create duplicate media records;
- probing the same file identity is deterministic;
- planning the same source and profile produces the same execution identity;
- claiming work is atomic;
- validation can safely run again;
- deleting a rejected candidate tolerates the file already being absent;
- promotion detects whether it already completed;
- recovery uses persisted facts and filesystem facts rather than guesses;
- stale workers cannot overwrite a newer job state;
- cancellation is safe when the process has already exited;
- cleanup does not delete the original media file.

Destructive operations require especially strong tests.

The original file remains authoritative until promotion completes successfully.

## 13. Concurrency and Resource Ownership

Avarch should model work by resource class rather than treating the scheduler as a single sequential
loop.

Representative classes:

- heavy encoding;
- lightweight probe or validation;
- promotion/filesystem mutation;
- cleanup.

Rules:

- only one component owns a running external process;
- process identifiers and attempt ownership are persisted where recovery requires them;
- workers claim jobs atomically;
- each stage has an explicit concurrency limit;
- validation and promotion can progress while another job encodes;
- promotion of the same source must never run concurrently;
- filesystem paths used for candidates must be unique per attempt or execution identity;
- scheduler stop must distinguish graceful stop from hard cancellation;
- a stop request must not be reported as completed until the owned process has actually stopped or
  a failure is recorded.

Concurrency must not be implemented through scattered status checks.

## 14. Configuration and Time

Configuration must enter through explicit application boundaries.

Domain functions receive resolved policy values rather than reading global configuration.

Bad:

```python
def validate_candidate(candidate):
    if settings.require_smaller_output:
        ...
```

Preferred:

```python
def evaluate_candidate(
    facts: ValidationFacts,
    policy: ValidationPolicy,
) -> ValidationReport:
    ...
```

The system clock is an external dependency.

Time-sensitive decisions should receive a timestamp or clock port, especially for:

- stale probe detection;
- leases;
- retries;
- attempt timing;
- abandoned job recovery;
- retention and cleanup.

This keeps tests deterministic.

## 15. Logging and Observability

Logging belongs in the imperative shell.

Domain functions return structured decisions; they do not emit operational logs.

Logs should include stable context where applicable:

- job identifier;
- attempt identifier;
- file identity;
- source path;
- candidate path;
- workflow stage;
- current and target state;
- external command;
- process identifier;
- outcome or failure category.

Avoid duplicate logging at every layer.

The layer that handles or translates an outcome should normally be responsible for logging it.

Do not parse human-readable log messages to derive application state.

Persisted state is authoritative.

## 16. Testing Strategy

The architecture must support three practical test levels.

### 16.1 Pure Core Tests

These are the majority of tests.

They cover:

- dimension calculations;
- profile resolution;
- deterministic planning;
- file eligibility;
- probe freshness;
- validation policy;
- size comparison;
- state transitions;
- cancellation decisions;
- recovery decisions;
- retry and skip policy;
- resource classification.

They require no database, subprocess, or filesystem unless the value under test is specifically
path-related.

### 16.2 Adapter Contract Tests

Contract tests verify boundary behavior.

Examples:

- two workers cannot claim the same job;
- a persisted transition checks the expected version;
- SQLite round-trips structured failure data;
- filesystem promotion preserves the original on failure;
- process runner captures exit code, stdout, stderr, and termination state;
- ffprobe output is translated into the expected internal facts.

Use a real temporary SQLite database and temporary filesystem where this provides more confidence
than mocks.

### 16.3 Workflow Tests

Workflow tests exercise complete use cases using:

- temporary filesystem;
- temporary SQLite database;
- fake or stub process runner;
- fake clock;
- deterministic fixture media metadata.

Representative behavior:

```python
def test_larger_candidate_is_deleted_and_original_is_preserved():
    ...
```

```python
def test_completed_encode_can_be_validated_while_next_job_encodes():
    ...
```

```python
def test_interrupted_validation_is_recovered_without_reencoding():
    ...
```

CLI tests should remain thin and primarily verify parsing, command dispatch, exit codes, and
user-facing rendering.

Do not test private implementation details merely to increase coverage.

## 17. Simplicity Rules

The refactor must reduce accidental complexity rather than merely move it.

The following are prohibited unless justified by a concrete current need:

- generic repository frameworks;
- base service classes;
- dependency injection containers;
- one interface per class;
- one file per trivial type;
- event sourcing;
- microservices;
- plugin systems for internal code;
- universal task or workflow abstractions;
- speculative extension points;
- deep inheritance;
- reflection-based dispatch;
- broad compatibility layers kept indefinitely;
- duplicate domain, DTO, persistence, and API models without behavioral value.

Prefer:

- direct function calls;
- small protocols;
- explicit composition;
- immutable values;
- ordinary modules;
- enums and tagged outcomes;
- straightforward control flow;
- deletion of obsolete code.

A new abstraction should make current code easier to understand or test. "We might need it later"
is not sufficient.

## 18. Architecture Rules for Contributors and Agents

Any contributor or coding agent performing the refactor must follow these rules:

1. Preserve observable behavior unless a requirement explicitly changes it.
2. Add characterization tests before moving poorly understood behavior.
3. Refactor one vertical workflow slice at a time.
4. Keep the test suite green after every slice.
5. Commit after every green slice.
6. Do not combine unrelated behavior changes with structural moves.
7. Do not introduce compatibility shims unless they are required and have a removal point.
8. Delete superseded modules after callers have migrated.
9. Do not leave two architectural paths for the same behavior.
10. Do not rename domain concepts casually.
11. Keep public CLI behavior stable unless the change is explicitly part of the task.
12. Do not bypass the state machine for convenience.
13. Do not allow adapter types to leak through application boundaries.
14. Do not introduce a framework to solve a local module-organization problem.
15. Record any deliberate deviation from this document as an architecture decision.

### 18.1 Port-and-Delete Rule

Every architecture port must include removal of the replaced path as part of the same green slice
whenever the callers have migrated.

When moving behavior into a new domain, application, adapter, or workflow module:

- migrate callers to the new owner;
- run the focused tests that prove the new path preserves behavior;
- delete the old function, module, re-export, compatibility facade, or duplicate workflow once it
    has no callers;
- search for imports and call sites of the old path before finishing;
- add or keep a boundary test when it prevents the old dependency direction from returning.

Temporary compatibility shims are allowed only when a slice cannot safely migrate all callers at
once. They must be small, explicitly transitional, covered by tests, and removed in the next slice
that migrates the remaining callers. A port is not complete while both old and new architectural
paths remain reachable for the same behavior.

When current behavior is ambiguous:

- inspect tests, schema, CLI behavior, and persisted state handling;
- preserve the safest behavior;
- add a characterization test;
- document the assumption in the commit or implementation notes.

## 19. Recommended Refactoring Sequence

The architectural overhaul should be incremental.

### Phase 1: Characterize Current Behavior

- Identify public CLI workflows.
- Map scheduler stages and status mutations.
- Locate all filesystem mutations.
- Locate all subprocess ownership.
- Locate all direct database access.
- Add tests around destructive and recovery-sensitive behavior.
- Establish a green baseline.

No major movement should happen before critical behavior is protected.

### Phase 2: Establish Package Boundaries

- Create the target top-level packages.
- Add `bootstrap.py` as the composition root.
- Move modules without changing behavior where possible.
- Update imports.
- Keep commits mechanical and reviewable.
- Add import-boundary checks if practical.

### Phase 3: Extract Pure Domain Policy

Extract and test:

- planning decisions;
- file eligibility;
- validation policy;
- output-size decisions;
- retry and skip decisions;
- recovery decisions;
- execution identity;
- state transitions.

Remove I/O from those functions.

### Phase 4: Centralize Job Transitions

- Define legal states and events.
- Route all status changes through transition logic.
- Make persisted updates atomic.
- Add stale-write protection.
- Add exhaustive transition tests.
- Remove direct status assignment outside the state persistence implementation.

### Phase 5: Separate Workflows from Adapters

- Define narrow ports.
- Move SQLite implementation under `adapters/sqlite`.
- Move external command integration into dedicated adapters.
- Make application workflows depend on ports.
- Remove SQLModel and subprocess knowledge from domain and application policy.

### Phase 6: Split Scheduler Stages

- Make encode, validate, promote, and cleanup independently claimable stages.
- Introduce explicit resource classes and limits.
- Preserve ownership and recovery rules.
- Verify validation and promotion can proceed while encoding continues.
- Verify stop and cancellation behavior.

### Phase 7: Remove Obsolete Structure

- Delete replaced modules.
- Remove temporary re-exports.
- Remove compatibility shims.
- Remove duplicate workflows.
- Update tests and documentation.
- Confirm the old flat architecture is no longer reachable.

Each phase should be split into small TDD slices with a green commit after each slice.

## 20. Definition of Done for the Architectural Overhaul

The overhaul is complete only when all of the following are true.

### Architecture

- The dependency direction in this document is enforced.
- Domain modules contain no filesystem, database, subprocess, CLI, or scheduler I/O.
- Application workflows depend on ports rather than concrete adapters.
- Concrete implementations are assembled in the composition root.
- Job state changes pass through the central transition model.
- Encoding, validation, promotion, and cleanup are separate workflow stages.
- No obsolete parallel architecture remains.

### Behavior

- Existing supported CLI workflows still behave as specified.
- Original media files remain safe through failure, cancellation, restart, and validation rejection.
- A candidate that is not smaller is discarded and the original is preserved.
- Validation and promotion may run while another encoding is active.
- Stop and cancellation reflect the real external process state.
- Interrupted work can be recovered deterministically.
- Durable stages are idempotent.

### Quality

- Tests cover domain policy, state transitions, destructive operations, concurrency claims, and
  recovery.
- The full test suite passes.
- Ruff passes.
- Pyright passes at the project's configured strictness.
- Migration and database tests pass.
- No circular imports remain.

## 21. Scheduler Watch and Studio Integration Notes

`scheduler watch` is intentionally a presentation and control boundary, not a
second scheduler runtime. The watch loop reads snapshots, samples host/container
telemetry outside SQLite, and delegates pause, resume, cancellation, details,
and log path resolution through application-layer ports. It must not call CLI
command functions or mutate scheduler state directly.

This split is the future Studio integration point. A graphical Studio surface
should consume the same snapshot models and control boundary, while keeping
keyboard input, terminal rendering, and Rich-specific layout code replaceable.
Logs stay as bounded tails and are never parsed into lifecycle events by the
dashboard. Resource telemetry remains Linux-first and informational: active
output growth is not a claim about physical disk throughput.
- No unused compatibility layers remain.
- No broad `utils`, `helpers`, or `manager` modules contain domain behavior.
- Documentation reflects the final package structure and lifecycle.

### Reviewability

- The refactor was delivered in small, green commits.
- Structural changes and behavior changes are separated.
- Each commit has a clear purpose.
- Removed code is actually deleted rather than left behind as an alternate path.

## 21. Architectural Decision Checklist

Before adding or changing code, ask:

1. Is this a decision or an effect?
2. Can the decision be represented as a pure function?
3. Which workflow owns this behavior?
4. Is this a domain outcome or an execution failure?
5. Is the state transition legal and centralized?
6. What happens if the operation runs twice?
7. What happens if the process stops halfway through?
8. Can a stale worker overwrite newer state?
9. Does this require a real port, or is a direct function call clearer?
10. Is the abstraction solving a current problem?
11. Does this change preserve the original file under every failure path?
12. Can the behavior be tested without launching real media tools?
13. Is there now more than one way to perform the same operation?
14. Can any obsolete code be deleted after this change?
15. If a compatibility shim remains, what exact follow-up slice removes it?

## 22. Final Guiding Principles

The Avarch architecture should be judged by these principles:

1. The domain contains decisions, not I/O.
2. The shell performs effects but does not invent policy.
3. Every durable state change is explicit and legal.
4. Expected outcomes are values; unexpected execution failures are errors.
5. External systems sit behind narrow, domain-specific ports.
6. Each workflow stage is independently retry-safe.
7. Original files remain authoritative until safe promotion completes.
8. Functions are preferred over stateless classes.
9. Abstractions must earn their existence.
10. The scheduler coordinates work; it does not become the entire application.
11. Persisted state is authoritative; logs are diagnostic.
12. A refactor is incomplete until the replaced code is removed.
13. Boring, explicit code is preferred over clever, generic code.
14. The architecture serves Avarch's workflow, not the other way around.
