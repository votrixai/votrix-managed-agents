---
title: Sandbox Runtime
description: Isolation boundaries, lifecycle, providers, and production sandbox requirements.
---

VMA maps a Claude Managed Agents `Environment` onto a Deep Agents backend. The sandbox is the security boundary for model-controlled code. Tool approval, filesystem middleware, a working directory, and model instructions are not substitutes for process isolation.

Useful upstream references:

- [Claude cloud environments](https://platform.claude.com/docs/en/managed-agents/environments)
- [Claude cloud sandbox reference](https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference)
- [E2B sandbox persistence](https://e2b.dev/docs/sandbox/persistence)
- [E2B sandbox auto-resume](https://e2b.dev/docs/sandbox/auto-resume)
- [E2B template user and workdir](https://e2b.dev/docs/template/user-and-workdir)

Current status is **partial**. VMA has a safe no-shell default, an optional E2B integration, and a custom-provider boundary. E2B is an external hosted service unless the operator deploys [E2B BYOC](https://e2b.dev/docs/byoc); neither option is a guarantee of Anthropic-equivalent infrastructure or behavior.

## Backend selection

`app.runtime.sandbox.open_backend()` selects one of these paths:

| Condition | Deep Agents backend | Shell execution | Policy enforcement |
| --- | --- | --- | --- |
| `VMA_SANDBOX_PROVIDER=e2b` | `AsyncE2BSandbox` | Yes, in one E2B sandbox per Session | E2B plus VMA policy and lifecycle checks |
| `VMA_SANDBOX_FACTORY` is configured | Operator-provided backend | Depends on the backend | Factory/provider responsibility |
| Environment type is `local` and `VMA_ALLOW_UNSAFE_LOCAL_SANDBOX=true` | `LocalShellBackend` | Yes, on the control-plane host | No isolation |
| Otherwise | `StateBackend` | No | Checkpointed file state only |

The safe default is Deep Agents' `StateBackend`. It can preserve virtual files through the configured LangGraph checkpointer, but it cannot execute commands, install packages, or enforce network and container policy.

## E2B setup

Install the pinned optional dependencies and select the provider explicitly:

```bash
uv sync --extra sandbox-e2b
```

```dotenv
VMA_SANDBOX_PROVIDER=e2b
E2B_API_KEY=...
VMA_E2B_TEMPLATE=vma-hardened
VMA_E2B_GUEST_USER=user
VMA_E2B_TEMPLATE_RESOURCES={"cpu":2,"memory_mb":2048}
```

The extra pins `langchain-e2b==0.0.5` and `e2b==2.31.0`. `E2B_API_KEY` belongs to the control plane and is never copied into a tenant sandbox or returned by the public API. `VMA_E2B_TEMPLATE` is required and must select an operator-owned hardened template.

VMA deliberately performs a narrow set of template privilege checks: the default execution user must match `VMA_E2B_GUEST_USER` and be non-root, passwordless `/usr/bin/sudo` must fail, trusted `/usr/bin/python3` must exist, and the guest must not be able to modify `/usr/bin` or `/usr/lib`. These checks run during bootstrap and before every turn. Root bootstrap and verification run with an empty environment through `/usr/bin/python3 -I -S`, so E2B's guest-writable `/usr/local` tree cannot shadow the control path. The template remains the trusted computing base for all other Linux filesystem, capability, package, and process hardening.

## One Session, one sealed sandbox

The built-in E2B lifecycle has three invariants:

1. One VMA Session is bound to exactly one opaque E2B `external_sandbox_id`.
2. Skills, Memory Store seeds, and create-time files are materialized during
   Session creation. A later file may only extend the immutable upload manifest
   through the idle-only append API; no existing input is replaced or removed.
3. A later turn reconnects that exact sandbox and verifies the latest seal. A
   turn never re-uploads or repairs existing control-plane inputs.

Session creation is therefore the provisioning boundary, not the first executable turn:

1. VMA resolves the Session's pinned agent version, Skills, read-only input files, and initial Memory Store seed.
2. It computes a deterministic create identity for the bundle, including the
   original read-write memory seed, plus revision `0` of the immutable-file
   manifest.
3. It provisions one E2B sandbox, uploads the bundle, makes control-plane-owned
   immutable files root-owned and non-writable to the guest, and writes a
   root-owned seal containing the expected digest, manifest, and revision.
4. It records the opaque E2B ID, owner and policy fingerprints, template, and input digest as private database state.
5. It pauses the sandbox with full-memory preservation until a turn needs it.

Provisioning failure aborts Session creation and attempts to kill the just-created sandbox. Database and E2B changes are not atomic, and VMA does not currently implement an orphan-recovery protocol; operators should monitor failed create requests and E2B inventory.

Alembic revision `20260711_0010` is a schema migration only: it creates the tenant-scoped one-to-one `session_sandboxes` binding table. It does not backfill, move, snapshot, or automatically migrate any sandbox data; a fresh database simply creates the table during normal schema setup.

While the active Session is idle, `sessions.resources.add` may append one file.
This includes an idle `requires_action` window in which `votrix-backend` is
executing a custom tool. The file must use exactly
`/mnt/session/uploads/<filename>` with no nested path. VMA locks the Session and
sandbox binding, validates tenant ownership and the copied object's size and
SHA-256, reconnects the same paused E2B sandbox, advances the sealed manifest
by one revision, pauses it again, and then commits the resource and manifest.
An exact retry of an already committed `(source file, mount path)` is
idempotent. A different file at the same or overlapping path is rejected.

For every subsequent turn, VMA:

1. Recomputes the latest input identity from the pinned Skills and Memory seed
   plus every committed create-time or appended file.
2. Loads the one stored external ID and reconnects that exact E2B sandbox.
3. Verifies the provider ownership/policy metadata, configured template,
   root-owned seal, latest manifest revision, immutable paths, permissions,
   hardlink count, and content hashes.
4. Passes the connected `AsyncE2BSandbox` to Deep Agents. The model invokes `execute` and filesystem operations through ordinary Deep Agents tool calls.
5. Before pausing, discovers eligible files directly below
   `/mnt/session/outputs` and reads their bounded bytes and filesystem metadata.
6. Pauses the same sandbox with full-memory preservation when the turn exits.
7. The runner validates the discovered batch and snapshots new
   `(path, SHA-256)` versions into R2-compatible storage as downloadable
   Session-scoped Files before committing the terminal/interrupt state.

VMA reconstructs the Python `AsyncSandbox`, `AsyncE2BSandbox`, and Deep Agents graph objects per run. It persists their identifiers and checkpoint state, not live Python objects.

If a Skill, existing input, initial read-only or read-write memory source, mount
identity, or configured template differs from the sealed identity, resume fails
and the caller must create a new Session. File addition is the sole mutation
exception: it must follow the append protocol above. Updates and deletion of
mounted inputs remain rejected. Skills and memory cannot be appended or
reseeded. There is no migration or replacement sandbox for the same Session.

PostgreSQL and E2B cannot participate in one transaction. VMA therefore
advances the provider seal before committing the database manifest. If the
process fails between those operations, retrying the exact same file bytes and
mount path can complete the compare-and-swap; the provider accepts either the
expected old seal or the exact already-advanced seal. Until that exact retry
repairs the database record, unrelated append and resume attempts fail closed.
VMA does not add an outbox, operation lease, automatic orphan recovery,
snapshot, or generation protocol for this window.

Archive pauses and preserves the bound sandbox. Session deletion kills it. E2B provider auto-resume is disabled so all reconnects pass through VMA authorization; secure access is enabled, inbound public traffic is disabled, and timeout handling uses full-memory pause.

The in-process janitor performs best-effort cleanup of eligible paused sandboxes after `VMA_SANDBOX_RETENTION_SECONDS`, which defaults to 30 days. This is not an exact provider retention guarantee: cleanup stops when all API processes are stopped or scaled to zero and resumes on a later process start.

## Filesystem contract

The sandbox has distinct immutable and mutable areas:

- Read-only create-time and append-only uploaded inputs are direct files below
  `/mnt/session/uploads`.
- Custom Skills are materialized below `/skills/custom` and are immutable for the Session.
- Read-only Memory Store seeds are immutable.
- `/workspace`, `/mnt/session/outputs`, and Memory Stores mounted with
  `read_write` access remain mutable inside the sandbox. The output root is
  guest-owned; it is not part of the immutable upload manifest.

Session files for E2B must be read-only and mounted as one normalized direct
child of `/mnt/session/uploads`. An immutable input cannot overlap `/workspace`,
`/mnt/session/outputs`, or a read-write memory root; the request is rejected
instead of relying on path precedence. Path normalization, archive traversal
checks, symlink and hardlink rejection, unmanaged-entry checks, and collision
checks run before activation or resume. Other backends retain the broader
public absolute-path contract; this E2B restriction is a documented runtime
difference.

Only Session creation and explicit append upload control-plane inputs. Resume
verifies the existing seal and contents but does not repair, replace, delete,
or re-upload anything. If verification fails, the turn fails closed. A provider
seal one revision ahead of PostgreSQL can only be recovered by the exact append
retry described above; other mismatches require a new Session.

Files changed under `/workspace` and read-write memory roots survive later turns
while the same E2B sandbox remains resumable, but they are not exported to
S3-compatible storage. In particular, edits to a read-write Memory Store seed
are **not** written back to VMA's managed Memory Store records or versions.

R2 or another S3-compatible store remains the durable source for uploaded file
and Skill content. It is not continuously synchronized with the sandbox. At the
end of each completed E2B graph execution, VMA exports only direct, regular,
single-link files below `/mnt/session/outputs`, subject to bounded file-count and
size limits. The current discovery boundary is 100 files, 50 MiB per file, and
100 MiB aggregate; bytes currently cross the E2B command channel as base64 JSON
until a streaming provider transfer is implemented. Exact `(Session, path,
SHA-256)` rediscovery is idempotent; changed bytes at the same path create a new
immutable File version. The Files API exposes these records through
`files.list(scope_id=<session_id>)`, metadata, and download. Nested files,
symlinks, hardlinks, directories, and files outside the approved output root
cause discovery to fail closed. Production malware
quarantine and content-disarm policy remain outside this implementation.

### Seal boundary

The current bootstrap protects control-plane-owned immutable files with root ownership, non-writable permissions, protected parent directories, and digest verification before every turn. This is a narrow guarantee: it does not prove that the guest can write only to `/workspace` and approved read-write memory roots, and it does not turn all other template files into immutable mounts.

The hardened E2B template removes unnecessary privilege-escalation paths and protects VMA's trusted roots, but E2B recreates provider-managed guest-writable paths such as `/usr/local`, `/code`, and `/home/user` when a sandbox starts. `/tmp` and `/var/tmp` also remain writable sticky scratch directories. These paths are Session-local and explicitly untrusted: VMA never executes root control code from them. Persistent private Agent work belongs under `/workspace` or an approved read-write memory root; generated files intended for object-storage export belong directly under `/mnt/session/outputs`. VMA validates the trusted system interpreter and directories and fails closed; it does not scan or harden the whole Linux filesystem. The current seal protects VMA-owned immutable inputs, not the entire guest filesystem.

## Network, packages, and resources

VMA maps environment networking as follows:

- `none`: E2B internet access is disabled.
- `limited`: E2B internet access is enabled with the requested destinations passed through `network.allow_out`.
- `unrestricted`: E2B internet access is enabled without an outbound allowlist.

Public inbound traffic remains disabled in every mode. Limited mode uses E2B's `allow_out` setting only; VMA does not add a separate `deny_out` or deny-all rule. Its effective guarantee therefore depends on the selected E2B SDK/service semantics and should be verified in the operator's deployment.

`networking.allow_mcp_servers` does not add E2B egress entries because VMA's MCP clients currently execute in the control-plane process, not in the sandbox.

E2B package and resource constraints are template-level:

- Non-empty per-Session package declarations are rejected; required packages must be built into `VMA_E2B_TEMPLATE`.
- Requested CPU, memory, and disk must match `VMA_E2B_TEMPLATE_RESOURCES`.
- VMA compares requests with the operator-declared template profile; it does not independently attest CPU, memory, or disk capacity in this MVP.
- A Session is rejected if it requests a different template, workdir, public-traffic setting, auto-resume behavior, or non-persistent pause behavior.

A custom provider must also fail closed when it cannot enforce a requested restriction.

## Deliberate scope

The E2B implementation deliberately does **not** include:

- Multiple sandbox generations per Session.
- A VMA snapshot or checkpoint migration layer for sandbox files.
- Daytona integration or automatic provider migration.
- Sandbox-operation leases, heartbeat/fencing, or durable outbox actions.
- Automatic orphan discovery or recovery after a control-plane crash.
- Per-turn bootstrap, repair, or managed-file synchronization.

These are documented omissions, not hidden lifecycle guarantees. LangGraph checkpoints still preserve the agent loop separately from E2B's persistent sandbox state.

## Production factory contract

Operators can instead configure a Python callable:

```dotenv
VMA_SANDBOX_FACTORY=my_service.sandboxes:create_backend
```

VMA calls it with `workspace_id`, `session_id`, and `environment_config`. It may return a Deep Agents backend, an awaitable, or an async context manager. A stateful provider should treat the workspace/Session pair as its stable private lookup key and must not assume that a Python backend object survives between requests or workers.

The factory is responsible for filesystem, process, network, package, resource, secret, retention, and deletion policy. It must not expose an external sandbox ID as a cross-tenant lookup capability.

## Tool permissions are not containment

Deep Agents filesystem permissions apply to its built-in file tools. Direct backend calls bypass that middleware, and shell commands can access files without using those tools. Treat `execute` as arbitrary code execution inside the sandbox and apply separate policy to MCP and HTTP tools.

Human approval controls workflow intent; it does not establish process or filesystem isolation.

## Unsafe local mode

Local execution is opt-in:

```dotenv
VMA_ALLOW_UNSAFE_LOCAL_SANDBOX=true
VMA_SANDBOX_ROOT=/workspace
```

This executes a shell on the control-plane host. Virtual path handling does not make it a container or enforce network/resource policy. Never enable it for untrusted tenants or in a multi-tenant deployment.

## Self-hosted work is not a sandbox

The optional `vma-worker` and environment work routes provide work-queue mechanics for `self_hosted` environments. They do not create or attest an isolated execution environment. A worker must still open an approved sandbox backend and enforce environment policy.

## Operational readiness checklist

Before enabling `execute` for tenants, verify:

1. The selected E2B template or custom provider supplies a real isolation boundary.
2. Cross-workspace and cross-Session access fails closed.
3. The guest cannot alter sealed inputs, provider metadata, control-plane credentials, or non-approved template paths.
4. Egress, metadata-service blocking, and public-traffic settings work in the deployed E2B environment.
5. Resource and command timeouts terminate work as expected.
6. Session create fails cleanly, and operators can detect any remote sandbox left by a crash between E2B creation and database commit.
7. Archive preserves the one sandbox, delete kills it, and best-effort 30-day cleanup is monitored.
8. Generated files are written as direct files below `/mnt/session/outputs`,
   stay within export size/count limits, and appear through scoped Files list
   and download; workloads do not rely on `/workspace` or Memory Store edits
   being exported.
9. Logs, traces, previews, and errors redact secrets and external provider identifiers.
10. Provider outages produce visible, retryable errors rather than silent reseeding or replacement.

Until those checks pass, VMA's Environment resource is a compatibility/control-plane object rather than Claude-equivalent managed infrastructure.
