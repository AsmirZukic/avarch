# Avarch Docker Migration and Custom VapourSynth Extension Plan

## 1. Objective

Transform Avarch into a Dockerized, CLI-only, workspace-first transcoding application while adding first-class support for:

```text
generated VapourSynth scripts
user-authored complete .vpy scripts
user-authored filter hooks
downloaded Python filter libraries
downloaded native VapourSynth plugins
workspace-local dependency manifests
reproducible job snapshots
```

The completed product must support this user workflow:

```bash
cd /mnt/nas/movies

avarch init
avarch scan .
avarch probe

avarch profiles copy default --name my_filtered_profile
avarch vpy scaffold filter --name my_filter

# Edit:
# .avarch/scripts/my_filter.py
# .avarch/profiles/my_filtered_profile.toml

avarch vpy sync
avarch vpy check \
  --profile my_filtered_profile \
  "Movies/Test.mkv"

avarch workflow preview \
  --profile my_filtered_profile

avarch workflow enqueue \
  --profile my_filtered_profile

avarch scheduler run
```

---

# 2. Final product architecture

## 2.1 Host

The host requires:

```text
Docker
the Avarch wrapper command
```

The host does not require:

```text
Python
Rust
FFmpeg
VapourSynth
Av1an
VSRepo
media plugins
```

## 2.2 Docker image

The official image contains:

```text
Avarch CLI
Python runtime
FFmpeg
ffprobe
VapourSynth
vspipe
Av1an
VSRepo
built-in profiles
baseline source plugins
baseline filtering plugins
public Avarch VapourSynth API
```

## 2.3 Workspace

Each media workspace owns:

```text
.avarch/
├── workspace.toml
├── config.toml
├── profiles/
├── scripts/
├── vpy/
│   ├── requirements.toml
│   ├── current
│   └── environments/
├── data/
│   └── avarch.db
├── logs/
├── work/
├── tmp/
└── run/
```

## 2.4 Scheduler

Each workspace has an independent:

```text
database
queue
job numbering
scheduler state
logs
work directories
promotion history
```

Only one scheduler container may execute on the Docker host.

The scheduler container name is:

```text
avarch-encoder
```

## 2.5 Built-in profiles

Built-ins are:

```text
packaged in the image
immutable
always available
name-reserved
```

Users create editable profiles with different names:

```bash
avarch profiles copy default --name my_default
```

## 2.6 Promotion

Supported outcomes remain:

```text
validated but not promoted
keep original
retain original as backup
atomic replace
```

Avarch never encodes directly over a source file.

---

# 3. Requirement groups

## R1 — Dockerized CLI runtime

Avarch must execute through a host wrapper that launches short-lived Docker containers.

The wrapper must:

```text
discover the workspace
mount it at /workspace
run as the invoking UID and GID
forward signals
preserve exit codes
select CPU or optional GPU access
enforce one machine-wide scheduler
```

## R2 — Workspace-local application state

All persistent Avarch state must be stored beneath:

```text
<workspace>/.avarch/
```

Media paths must be persisted relative to the workspace.

## R3 — Workspace-isolated queues

Workspace A and Workspace B must be able to contain:

```text
job 1
job 2
job 3
```

without sharing or colliding.

Running Workspace A’s scheduler must not inspect or mutate Workspace B.

## R4 — Immutable reserved built-in profiles

A workspace cannot define a user profile named:

```text
default
anime
web_archive
```

when those names belong to built-ins.

Profile collisions must fail instead of overriding.

## R5 — Generated VapourSynth mode

Avarch generates the complete `.vpy` graph.

Avarch owns:

```text
source loading
cache and index paths
resize
format normalization
output registration
```

## R6 — Custom filter-hook mode

The user writes a Python filter function:

```python
def apply(
    video: vs.VideoNode,
    context: FilterContext,
) -> vs.VideoNode:
    ...
```

Avarch owns the surrounding graph.

## R7 — Full custom-template mode

The user owns the complete `.vpy` graph.

The custom template must:

```text
load the source
apply processing
register output index 0
```

Avarch supplies stable context variables.

## R8 — Workspace Python dependencies

Users can install downloaded Python filter libraries into a workspace-local environment.

## R9 — Workspace native VapourSynth plugins

Users can install compatible native plugins into a workspace-local environment.

## R10 — Reproducible planning

Every job must record:

```text
Avarch image digest
VapourSynth version
script hash
profile effective hash
dependency environment identity
native plugin hashes
Python package versions
template API version
generated-wrapper version
```

## R11 — Validation and diagnostics

Users need commands for:

```text
static script validation
runtime script evaluation
plugin listing
environment inspection
dependency synchronization
missing-dependency diagnostics
```

## R12 — Automated full-stack validation

The final suite must validate:

```text
two isolated workspaces
Docker wrapper behavior
machine-wide scheduler exclusion
custom filter execution
custom template execution
Python dependency loading
native plugin loading
real tiny encode
validation
promotion
```

---

# 4. VapourSynth modes

## 4.1 Generated mode

Profile:

```toml
[vapoursynth]
mode = "generated"
```

Avarch generates:

```text
source loading
processing selected by profile
resize
format normalization
set_output(0)
```

## 4.2 Custom filter mode

Profile:

```toml
[vapoursynth]
mode = "custom_filter"
script = "my_filter.py"
entrypoint = "apply"
api_version = 1
```

Script:

```python
from __future__ import annotations

import vapoursynth as vs

from avarch.vpy_api import FilterContext


def apply(
    video: vs.VideoNode,
    context: FilterContext,
) -> vs.VideoNode:
    return video
```

Avarch:

```text
loads the source
constructs FilterContext
loads the user module
calls apply()
validates returned VideoNode
performs final profile normalization
registers output 0
```

## 4.3 Custom template mode

Profile:

```toml
[vapoursynth]
mode = "custom_template"
template = "my_pipeline.vpy"
api_version = 1
```

The template receives variables such as:

```text
AVARCH_TEMPLATE_API_VERSION
AVARCH_SOURCE_PATH
AVARCH_SOURCE_RELATIVE_PATH
AVARCH_VIDEO_STREAM_INDEX
AVARCH_SOURCE_WIDTH
AVARCH_SOURCE_HEIGHT
AVARCH_TARGET_WIDTH
AVARCH_TARGET_HEIGHT
AVARCH_SOURCE_FPS_NUM
AVARCH_SOURCE_FPS_DEN
AVARCH_SOURCE_IS_HDR
AVARCH_COLOR_PRIMARIES
AVARCH_COLOR_TRANSFER
AVARCH_COLOR_MATRIX
AVARCH_WORK_DIR
AVARCH_CACHE_DIR
```

The user script must register:

```python
video.set_output(0)
```

---

# 5. Workspace VapourSynth environment

## 5.1 Layout

```text
.avarch/vpy/
├── requirements.toml
├── current
└── environments/
    └── <environment-id>/
        ├── python/
        ├── plugins/
        ├── libraries/
        ├── assets/
        └── lock.toml
```

## 5.2 Runtime environment

Before evaluating a script, Avarch sets:

```text
PYTHONPATH=
  /workspace/.avarch/scripts
  /workspace/.avarch/vpy/environments/<id>/python

VAPOURSYNTH_EXTRA_PLUGIN_PATH=
  /workspace/.avarch/vpy/environments/<id>/plugins

LD_LIBRARY_PATH=
  /workspace/.avarch/vpy/environments/<id>/libraries
  plus image defaults
```

## 5.3 Manifest

`.avarch/vpy/requirements.toml`:

```toml
schema_version = 1

[python]
packages = [
  "some-filter-library==2.4.1",
]

[vsrepo]
packages = [
  "fmtconv",
  "bm3d",
]

[assets]
paths = [
  "models",
]
```

## 5.4 Lock

Generated environment lock:

```toml
schema_version = 1

environment_id = "linux-amd64-python312-vs76-abc123"
avarch_image_digest = "sha256:..."
vapoursynth_version = 76
python_version = "3.12"
platform = "linux-amd64"

[[packages]]
name = "some-filter-library"
kind = "python"
version = "2.4.1"
sha256 = "..."

[[packages]]
name = "fmtconv"
kind = "native"
version = "..."
namespace = "fmtc"
sha256 = "..."
```

---

# 6. Environment compatibility

The environment identity must include:

```text
operating system
CPU architecture
Python ABI
VapourSynth API version
Avarch image digest
dependency lock
```

Example:

```text
linux-amd64-python312-vs76-5f80c8e1
```

A workspace environment built for another image must not be activated silently.

Required error:

```text
The active VapourSynth environment was created for another
Avarch runtime.

Run:

  avarch vpy sync
```

---

# 7. Job materialization

For every planned job, create:

```text
.avarch/work/jobs/<job-id>/
├── plan.json
├── video.vpy
├── vpy/
│   ├── user_filter.py
│   ├── custom_template.vpy
│   ├── environment-lock.toml
│   └── snapshot.json
├── logs/
├── cache/
└── output/
```

Only files relevant to the selected mode are created.

## 7.1 Generated mode

Create:

```text
video.vpy
environment-lock.toml
snapshot.json
```

## 7.2 Custom filter mode

Copy the exact filter source:

```text
vpy/user_filter.py
```

Generate the wrapper as:

```text
video.vpy
```

## 7.3 Custom template mode

Copy the exact template:

```text
vpy/custom_template.vpy
```

Materialize a deterministic preamble and final executable script:

```text
video.vpy
```

## 7.4 Retry behavior

Retries use the job snapshot.

They must not silently use:

```text
edited workspace scripts
new dependency versions
new native plugin binaries
another image digest
```

When the environment no longer matches, require re-planning.

---

# 8. User-facing commands

## Environment

```bash
avarch vpy env show
avarch vpy env check
avarch vpy sync
```

## Packages

```bash
avarch vpy packages search <query>
avarch vpy packages list
avarch vpy packages install <name>
avarch vpy packages remove <name>
```

Package install and remove update the requirements manifest and then run synchronization.

## Plugins

```bash
avarch vpy plugins
```

Displays:

```text
namespace
plugin name
version
source
path
```

## Scaffolding

```bash
avarch vpy scaffold filter \
  --name my_filter
```

Creates:

```text
.avarch/scripts/my_filter.py
```

```bash
avarch vpy scaffold template \
  --name my_pipeline
```

Creates:

```text
.avarch/scripts/my_pipeline.vpy
```

```bash
avarch profiles scaffold \
  --from default \
  --name my_profile \
  --filter my_filter.py
```

Creates the profile and filter atomically.

## Validation

```bash
avarch vpy validate \
  --profile my_profile
```

Static checks only.

```bash
avarch vpy check \
  --profile my_profile \
  "Movies/Test.mkv"
```

Runtime checks against a real file.

---

# 9. Implementation sequence

The work is divided into twelve milestones.

---

# Milestone 1 — CLI-only baseline

## Goal

Create a clean CLI-only codebase before changing runtime and paths.

## Entry criteria

```text
current repository builds
current CLI commands can run
existing scheduler, validation, and promotion tests are known
```

## Slice 1.1 — Remove Textual

Remove:

```text
TUI command
Textual dependency
TUI source
TUI tests
TUI documentation
```

### Tests

```python
def test_cli_has_no_tui_command() -> None:
    ...


def test_textual_is_not_installed_as_runtime_dependency() -> None:
    ...
```

### Commit

```bash
git commit -m "refactor(cli): remove textual interface"
```

## Slice 1.2 — Establish final command hierarchy

Add or normalize:

```text
workspace
config
scan
probe
workflow
scheduler
jobs
queue
profiles
vpy
promote
doctor
version
```

### Commit

```bash
git commit -m "refactor(cli): define docker workspace command surface"
```

## Exit criteria

```text
CLI-only application passes tests
no TUI or Textual code remains
```

---

# Milestone 2 — Workspace foundation

## Goal

Make the workspace the root of all application paths.

## Slice 2.1 — Workspace context

Add:

```python
@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    ...
```

### Tests

```python
def test_workspace_derives_all_paths() -> None:
    ...


def test_workspace_path_cannot_escape_root() -> None:
    ...
```

### Commit

```bash
git commit -m "feat(workspace): define workspace context"
```

## Slice 2.2 — Workspace metadata

Implement:

```text
.avarch/workspace.toml
workspace UUID
schema version
```

### Commit

```bash
git commit -m "feat(workspace): add stable workspace identity"
```

## Slice 2.3 — Initialization

Implement:

```bash
avarch init
```

### Tests

```python
def test_init_creates_complete_layout() -> None:
    ...


def test_init_is_atomic() -> None:
    ...


def test_init_does_not_overwrite_workspace() -> None:
    ...
```

### Commit

```bash
git commit -m "feat(workspace): initialize local workspace"
```

## Slice 2.4 — Relative paths

Replace durable absolute media paths with workspace-relative paths.

### Commit

```bash
git commit -m "refactor(media): persist workspace-relative paths"
```

## Exit criteria

```text
all persistent paths derive from one workspace
workspace can be moved without rewriting media records
```

---

# Milestone 3 — Database and local job isolation

## Goal

Make one current database fully workspace-local.

## Slice 3.1 — Current initial migration

Update only:

```text
migrations/versions/0001_initial_schema.py
```

### Tests

```python
def test_only_initial_migration_exists() -> None:
    ...


def test_fresh_workspace_contains_current_schema() -> None:
    ...
```

### Commit

```bash
git commit -m "refactor(database): baseline workspace schema"
```

## Slice 3.2 — SQLite safety

Configure:

```text
foreign_keys ON
busy_timeout
journal_mode DELETE
synchronous FULL
```

### Commit

```bash
git commit -m "fix(database): configure workspace-safe sqlite"
```

## Slice 3.3 — Cross-workspace isolation

Create two workspaces with the same local job IDs.

### Tests

```python
def test_same_job_id_can_exist_in_two_workspaces() -> None:
    ...


def test_workspace_a_never_reads_workspace_b_jobs() -> None:
    ...
```

### Commit

```bash
git commit -m "test(database): prove workspace job isolation"
```

## Exit criteria

```text
one database per workspace
job IDs are local
no cross-workspace state access
```

---

# Milestone 4 — Profile registry

## Goal

Implement immutable reserved built-ins and editable uniquely named user profiles.

## Slice 4.1 — Package built-ins

### Commit

```bash
git commit -m "feat(profiles): package immutable built-ins"
```

## Slice 4.2 — Workspace profiles

Discover:

```text
.avarch/profiles/**/*.toml
```

### Commit

```bash
git commit -m "feat(profiles): discover workspace profiles"
```

## Slice 4.3 — Reserved names

### Tests

```python
def test_user_profile_cannot_use_builtin_name() -> None:
    ...


def test_collision_does_not_override_builtin() -> None:
    ...
```

### Commit

```bash
git commit -m "feat(profiles): reserve built-in names"
```

## Slice 4.4 — Copy and scaffold

Implement:

```bash
avarch profiles copy default --name my_default
avarch profiles scaffold --from default --name my_profile
```

### Commit

```bash
git commit -m "feat(profiles): create editable workspace profiles"
```

## Exit criteria

```text
built-ins are immutable
collisions fail
user profiles require different names
```

---

# Milestone 5 — Docker runtime migration

## Goal

Build a self-contained image and host wrapper.

## Slice 5.1 — Runtime image

Create multi-stage Docker build.

### Image checks

```bash
docker run --rm avarch:test version
docker run --rm avarch:test doctor
```

### Commit

```bash
git commit -m "build(docker): add avarch runtime image"
```

## Slice 5.2 — Media toolchain

Include:

```text
FFmpeg
ffprobe
VapourSynth
vspipe
Av1an
VSRepo
baseline plugins
```

### Commit

```bash
git commit -m "build(docker): include encoding toolchain"
```

## Slice 5.3 — Non-root execution

Validate arbitrary UID/GID.

### Commit

```bash
git commit -m "fix(docker): preserve host file ownership"
```

## Slice 5.4 — Workspace wrapper

Implement host workspace discovery and mounting.

### Commit

```bash
git commit -m "feat(wrapper): run commands in workspace container"
```

## Slice 5.5 — Machine encoder exclusion

Use fixed scheduler container name:

```text
avarch-encoder
```

### Tests

```text
first scheduler starts
second scheduler is rejected
owner workspace is reported
stale Avarch container is removed
foreign container is preserved
```

### Commit

```bash
git commit -m "feat(wrapper): enforce one machine encoder"
```

## Slice 5.6 — GPU options

Support:

```text
CPU
AVARCH_GPU=nvidia
AVARCH_GPU=dri
```

### Commit

```bash
git commit -m "feat(wrapper): configure optional gpu access"
```

## Exit criteria

```text
host only needs Docker
wrapper mounts one workspace
files use host ownership
only one scheduler runs machine-wide
```

---

# Milestone 6 — Generated VapourSynth scripts

## Goal

Make Avarch’s generated script output deterministic and inspectable.

## Slice 6.1 — Public context models

Add:

```text
FilterContext
template context
API version constants
```

### Commit

```bash
git commit -m "feat(vpy): define public script context"
```

## Slice 6.2 — Generated wrapper builder

Implement a pure script generator.

### Tests

```python
def test_generated_script_is_deterministic() -> None:
    ...


def test_generated_script_registers_output_zero() -> None:
    ...


def test_generated_script_uses_workspace_paths() -> None:
    ...
```

### Commit

```bash
git commit -m "feat(vpy): generate deterministic video scripts"
```

## Slice 6.3 — Job materialization

Create:

```text
video.vpy
snapshot.json
environment-lock.toml
```

### Commit

```bash
git commit -m "feat(vpy): materialize job video graph"
```

## Slice 6.4 — Generated script inspection

Expose generated script through:

```bash
avarch jobs show <id>
```

and an optional artifact path.

### Commit

```bash
git commit -m "feat(jobs): expose generated video script"
```

## Exit criteria

```text
generated scripts are deterministic
scripts are job artifacts
source and output paths stay inside workspace
```

---

# Milestone 7 — Custom filter and template support

## Goal

Allow custom scripts using dependencies already included in the image.

## Slice 7.1 — Script path boundary

Resolve scripts only beneath:

```text
.avarch/scripts/
```

Reject escapes and unsafe symlinks.

### Commit

```bash
git commit -m "feat(vpy): enforce workspace script boundaries"
```

## Slice 7.2 — Custom filter loader

Implement:

```text
custom_filter mode
entrypoint loading
VideoNode return validation
```

### Tests

```python
def test_custom_filter_is_called() -> None:
    ...


def test_custom_filter_must_return_video_node() -> None:
    ...


def test_custom_filter_must_not_register_output() -> None:
    ...
```

### Commit

```bash
git commit -m "feat(vpy): support custom filter hooks"
```

## Slice 7.3 — Custom template materialization

Implement stable preamble variables and output validation.

### Commit

```bash
git commit -m "feat(vpy): support full custom templates"
```

## Slice 7.4 — Script scaffolding

Implement:

```bash
avarch vpy scaffold filter --name my_filter
avarch vpy scaffold template --name my_pipeline
```

### Commit

```bash
git commit -m "feat(vpy): scaffold custom scripts"
```

## Slice 7.5 — Paired profile and script scaffold

Create files atomically.

### Commit

```bash
git commit -m "feat(vpy): scaffold profile and script pair"
```

## Exit criteria

```text
users can execute custom scripts using image-shipped dependencies
custom scripts are copied into job snapshots
scaffolds never overwrite files
```

---

# Milestone 8 — Static and runtime script validation

## Goal

Catch failures before expensive encoding.

## Slice 8.1 — Static validation

Check:

```text
file exists
UTF-8
Python syntax
API version
entrypoint
template path
profile mode consistency
```

Command:

```bash
avarch vpy validate --profile my_profile
```

### Commit

```bash
git commit -m "feat(vpy): validate custom scripts statically"
```

## Slice 8.2 — Runtime check

Command:

```bash
avarch vpy check \
  --profile my_profile \
  "Movies/Test.mkv"
```

Run:

```text
probe
wrapper generation
vspipe evaluation
output 0 inspection
bounded frame request
```

### Commit

```bash
git commit -m "feat(vpy): evaluate scripts against media"
```

## Slice 8.3 — Plugin inventory

Implement:

```bash
avarch vpy plugins
```

### Commit

```bash
git commit -m "feat(vpy): report available plugin namespaces"
```

## Slice 8.4 — Clear diagnostics

Recognize:

```text
missing Python module
missing plugin namespace
missing shared library
script exception
invalid output type
missing output index
```

### Commit

```bash
git commit -m "feat(vpy): diagnose script dependency failures"
```

## Exit criteria

```text
users can validate scripts before queueing
missing dependencies produce actionable errors
```

---

# Milestone 9 — Workspace Python dependency environments

## Goal

Support downloaded Python filter libraries without rebuilding the image.

## Slice 9.1 — Requirements manifest

Define strict:

```text
.avarch/vpy/requirements.toml
```

### Commit

```bash
git commit -m "feat(vpy): define workspace dependency manifest"
```

## Slice 9.2 — Environment identity

Hash:

```text
manifest
image digest
Python ABI
VapourSynth version
platform
```

### Commit

```bash
git commit -m "feat(vpy): identify compatible extension environments"
```

## Slice 9.3 — Staged Python installation

Install into staging:

```text
environments/<id>.staging/python
```

Then validate and atomically activate.

### Commit

```bash
git commit -m "feat(vpy): install workspace python dependencies"
```

## Slice 9.4 — Lock file

Record resolved versions and hashes.

### Commit

```bash
git commit -m "feat(vpy): lock workspace python environment"
```

## Slice 9.5 — Runtime injection

Set workspace `PYTHONPATH` for:

```text
vspipe
Av1an
script checks
```

### Commit

```bash
git commit -m "feat(vpy): activate workspace python environment"
```

## Slice 9.6 — Package commands

Implement:

```bash
avarch vpy packages list
avarch vpy packages install
avarch vpy packages remove
avarch vpy sync
```

### Commit

```bash
git commit -m "feat(vpy): manage python filter packages"
```

## Exit criteria

```text
downloaded Python filter packages load inside scripts
environment creation is atomic
resolved dependencies are locked
```

---

# Milestone 10 — Native VapourSynth plugin environments

## Goal

Support compatible downloaded native plugins.

## Slice 10.1 — VSRepo integration

Wrap VSRepo through Avarch.

Do not expose raw internal installation paths.

### Commit

```bash
git commit -m "feat(vpy): integrate vsrepo package resolution"
```

## Slice 10.2 — Staged native installation

Install into:

```text
plugins/
libraries/
```

inside the environment staging directory.

### Commit

```bash
git commit -m "feat(vpy): install workspace native plugins"
```

## Slice 10.3 — Native compatibility check

Validate:

```text
architecture
ELF format
VapourSynth load
namespace availability
shared-library dependencies
```

### Commit

```bash
git commit -m "feat(vpy): validate native plugin compatibility"
```

## Slice 10.4 — Runtime plugin paths

Inject:

```text
VAPOURSYNTH_EXTRA_PLUGIN_PATH
LD_LIBRARY_PATH
```

### Commit

```bash
git commit -m "feat(vpy): activate workspace native plugins"
```

## Slice 10.5 — Lock native dependencies

Record:

```text
plugin file hash
namespace
version
source
shared-library metadata
```

### Commit

```bash
git commit -m "feat(vpy): lock native plugin environment"
```

## Slice 10.6 — Package search

Implement:

```bash
avarch vpy packages search <query>
```

### Commit

```bash
git commit -m "feat(vpy): search available plugin packages"
```

## Exit criteria

```text
compatible native plugins install workspace-locally
VapourSynth discovers their namespaces
incompatible binaries fail before encoding
```

---

# Milestone 11 — Reproducible script execution

## Goal

Ensure queued jobs cannot change when scripts or dependencies change.

## Slice 11.1 — Script content hashes

Include script hashes in effective work identity.

### Commit

```bash
git commit -m "feat(planner): include script identity in work hash"
```

## Slice 11.2 — Environment identity in plans

Include:

```text
environment ID
image digest
VapourSynth version
plugin lock hash
```

### Commit

```bash
git commit -m "feat(planner): snapshot vpy environment identity"
```

## Slice 11.3 — Execution-time verification

Before running:

```text
verify active environment
verify script snapshot
verify image identity
```

### Commit

```bash
git commit -m "feat(execution): verify vpy environment before encode"
```

## Slice 11.4 — Stale environment behavior

When the environment changes:

```text
do not silently run
keep job unchanged
require replan
```

### Commit

```bash
git commit -m "feat(jobs): reject stale vpy environments"
```

## Exit criteria

```text
editing a script does not alter an existing job
dependency upgrades do not alter an existing job
retries remain reproducible
```

---

# Milestone 12 — Runtime compatibility and full validation

## Goal

Prove the full Dockerized product and report runtime compatibility clearly.

## Slice 12.1 — Runtime compatibility command

Implement:

```bash
avarch doctor
avarch vpy env check
```

Report:

```text
image digest
architecture
Python version
VapourSynth version
active environment
native namespace list
```

### Commit

```bash
git commit -m "feat(doctor): inspect container vpy runtime"
```

## Slice 12.2 — Docker acceptance environment

Create:

```text
Workspace A
Workspace B
custom scripts
Python dependency fixture
native plugin fixture
tiny real media
```

### Commit

```bash
git commit -m "test(e2e): provision docker workspace fixtures"
```

## Slice 12.3 — Custom filter acceptance test

Prove:

```text
profile uses custom_filter
workspace Python package imports
native namespace loads
job encodes
validation passes
promotion succeeds
```

### Commit

```bash
git commit -m "test(e2e): validate custom filter workflow"
```

## Slice 12.4 — Custom template acceptance test

Prove:

```text
template loads source
template registers output 0
Av1an consumes script
validation passes
```

### Commit

```bash
git commit -m "test(e2e): validate custom template workflow"
```

## Slice 12.5 — Cross-workspace scheduler test

Prove:

```text
Workspace A starts encoder
Workspace B is rejected
A finishes
B can then start
jobs never cross workspaces
```

### Commit

```bash
git commit -m "test(e2e): validate machine encoder isolation"
```

## Slice 12.6 — Real tiny encode

Use real:

```text
FFmpeg
VapourSynth
vspipe
Av1an
encoder
```

### Commit

```bash
git commit -m "test(e2e): validate real docker encode"
```

## Exit criteria

```text
official image handles normal extensions
custom filter and template workflows pass end to end
two workspaces remain isolated
one scheduler is enforced machine-wide
```

---

# 10. Deferred requirements

Do not include in the first implementation unless required by a real filter.

## Dependency bundles

Deferred:

```text
multi-file filter bundles
automatic recursive import discovery
workspace Python projects
editable package installs
```

Initial filter hooks should be one self-contained user file plus declared packages.

## Arbitrary native binary downloads

Do not allow users to place arbitrary native binaries in `.avarch/scripts`.

Native plugins must be installed through:

```text
the managed package system
```

## Automatic system-package installation

Workspace dependency synchronization must not run:

```text
apt
dnf
zypper
apk
```

System dependencies are deferred to a future version.

## Derived or custom runtime images

Deferred:

```text
custom images for CUDA libraries
custom images for compilation toolchains
custom images for nonportable system runtimes
custom image selection through AVARCH_IMAGE
documentation for derived-image workflows
```

The first implementation only supports the official Avarch runtime image.

## Cross-workspace global dispatcher

There is no global queue.

The machine scheduler restriction does not select the next workspace automatically.

## Multiple simultaneous encoders

Encoder capacity remains:

```text
1
```

---

# 11. Entry criteria

The combined program starts when:

```text
current CLI tests can run
scheduler can process a job
planner can produce immutable plans
validation can inspect output
promotion modes work
Docker is available in development
current profile schema is known
current database schema is known
```

Before starting:

```bash
git status --short
uv sync
uv run ruff check .
uv run pyright
uv run pytest
```

Document unrelated existing failures.

---

# 12. Per-slice validation gate

Before every commit:

```bash
git diff --check
uv run ruff check .
uv run pyright
uv run pytest
```

For Docker slices:

```bash
docker build -t avarch:test .
docker run --rm avarch:test version
docker run --rm avarch:test doctor
```

For VapourSynth slices:

```bash
docker run --rm \
  -v "$workspace:/workspace" \
  -w /workspace \
  avarch:test \
  vpy check \
  --profile acceptance_profile \
  Test.mkv
```

Stage explicit paths:

```bash
git add <explicit paths>
git diff --cached --check
git diff --cached --stat
git commit -m "<message>"
git status --short
```

---

# 13. Final exit criteria

The program is complete only when all conditions pass.

## Docker

```text
Avarch runs entirely through Docker.
The host wrapper discovers and mounts the workspace.
The image contains the complete baseline media toolchain.
Files are written as the invoking host user.
Signals and exit codes propagate correctly.
```

## Workspaces

```text
Each workspace has one database.
All persistent application state is inside .avarch.
Media paths are workspace-relative.
Two workspaces may have identical local job IDs safely.
Jobs never cross workspace boundaries.
```

## Scheduler

```text
Only one avarch-encoder container may run.
A second workspace receives a clear ownership error.
Only one encode stage runs at a time.
Pause, resume, drain, and stop work through short-lived commands.
```

## Profiles

```text
Built-in profiles are immutable.
Built-in names are reserved.
User profiles require different names.
Built-ins can be copied to editable workspace profiles.
```

## VapourSynth scripts

```text
Generated mode works.
Custom filter mode works.
Custom template mode works.
Scaffolding creates valid starting files.
Script paths cannot escape the workspace.
```

## Dependencies

```text
Workspace Python packages can be synchronized and imported.
Compatible native plugins can be synchronized and loaded.
Dependency environments are versioned and locked.
Incompatible environments are rejected.
```

## Reproducibility

```text
Jobs snapshot script content.
Jobs snapshot profile identity.
Jobs snapshot dependency environment identity.
Jobs snapshot image identity.
Retries cannot silently use modified scripts or dependencies.
```

## Validation

```text
Static script validation works.
Runtime script checking works.
Plugin namespaces can be listed.
Missing modules and plugins have actionable diagnostics.
```

## Promotion

```text
Validated-only state works.
Keep-original works.
Retain-backup works.
Atomic replace works.
Interrupted promotion recovery works.
```

## Full-stack tests

```text
Custom filter workflow passes.
Custom template workflow passes.
Python dependency workflow passes.
Native plugin workflow passes.
Cross-workspace isolation passes.
Machine encoder exclusion passes.
Real tiny encode passes.
Validation and promotion pass.
```

---

# 14. Final definition of done

```text
A user with Docker installed can initialize an Avarch workspace, scan
media, copy a reserved built-in profile under a new name, create or
download a custom VapourSynth script, declare its Python and native
plugin dependencies, synchronize a workspace-local extension
environment, validate the script against a real media file, enqueue a
job, and encode it through the Dockerized scheduler.

Avarch generates a deterministic job-specific .vpy wrapper or
materializes the user’s full template. The job records the exact
profile, script, dependency environment, plugin hashes, VapourSynth
version, and Docker image identity used during planning.

Another workspace may contain an independent queue and identical local
job IDs, but Docker prevents both workspaces from running scheduler
containers at the same time.

The validated output may remain for manual review, be installed while
preserving the source, replace the source while retaining a backup, or
replace the source atomically through the existing promotion
transaction.
```
