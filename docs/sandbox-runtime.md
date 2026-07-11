# Sandbox Runtime

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
```

The extra pins `langchain-e2b==0.0.5` and `e2b==2.31.0`. `E2B_API_KEY` belongs to the control plane and is never copied into a tenant sandbox or returned by the public API. `VMA_E2B_TEMPLATE` is required and must select an operator-owned hardened template.

VMA deliberately performs only two template privilege checks: the default execution user must match `VMA_E2B_GUEST_USER` and must be non-root, and `sudo -n true` must fail. These checks run during bootstrap and before every turn. The template remains the trusted computing base for all other Linux filesystem, capability, package, and process hardening.

## One Session, one sealed sandbox

The built-in E2B lifecycle has three invariants:

1. One VMA Session is bound to exactly one opaque E2B `external_sandbox_id`.
2. Skills and initial resources are materialized only during Session creation.
3. A later turn reconnects that exact sandbox and never uploads or synchronizes control-plane files again.

Session creation is therefore the provisioning boundary, not the first executable turn:

1. VMA resolves the Session's pinned agent version, Skills, read-only input files, and initial Memory Store seed.
2. It computes a deterministic identity for the entire create-time bundle, including the original read-write memory seed, plus a separate immutable-file manifest.
3. It provisions one E2B sandbox, uploads the bundle once, makes control-plane-owned immutable files root-owned and non-writable to the guest, and writes a root-owned seal containing the expected digest.
4. It records the opaque E2B ID, owner and policy fingerprints, template, and input digest as private database state.
5. It pauses the sandbox with full-memory preservation until a turn needs it.

Provisioning failure aborts Session creation and attempts to kill the just-created sandbox. Database and E2B changes are not atomic, and VMA does not currently implement an orphan-recovery protocol; operators should monitor failed create requests and E2B inventory.

Alembic revision `20260711_0010` is a schema migration only: it creates the tenant-scoped one-to-one `session_sandboxes` binding table. It does not backfill, move, snapshot, or automatically migrate any sandbox data; a fresh database simply creates the table during normal schema setup.

For every subsequent turn, VMA:

1. Recomputes the fixed create-time input identity from the pinned Session configuration, including the original read-write memory seed.
2. Loads the one stored external ID and reconnects that exact E2B sandbox.
3. Verifies the provider ownership/policy metadata, configured template, root-owned seal, immutable paths, permissions, and content hashes.
4. Passes the connected `AsyncE2BSandbox` to Deep Agents. The model invokes `execute` and filesystem operations through ordinary Deep Agents tool calls.
5. Pauses the same sandbox with full-memory preservation when the turn exits.

VMA reconstructs the Python `AsyncSandbox`, `AsyncE2BSandbox`, and Deep Agents graph objects per run. It persists their identifiers and checkpoint state, not live Python objects.

If a Skill, initial input, initial read-only or read-write memory source, mount identity, or configured template differs from the create-time identity, resume fails and the caller must create a new Session. Once a managed sandbox is bound, APIs that would mutate Session resources are rejected. There is no in-place reseed, migration, or replacement sandbox for the same Session.

Archive pauses and preserves the bound sandbox. Session deletion kills it. E2B provider auto-resume is disabled so all reconnects pass through VMA authorization; secure access is enabled, inbound public traffic is disabled, and timeout handling uses full-memory pause.

The in-process janitor performs best-effort cleanup of eligible paused sandboxes after `VMA_SANDBOX_RETENTION_SECONDS`, which defaults to 30 days. This is not an exact provider retention guarantee: cleanup stops when all API processes are stopped or scaled to zero and resumes on a later process start.

## Filesystem contract

The initial bundle has distinct immutable and mutable areas:

- Read-only uploaded inputs default below `/mnt/session/uploads`.
- Custom Skills are materialized below `/skills/custom` and are immutable for the Session.
- Read-only Memory Store seeds are immutable.
- `/workspace` and Memory Stores mounted with `read_write` access remain mutable inside the sandbox.

Initial Session files for E2B must be read-only and mounted below `/mnt/session`. An immutable input cannot overlap `/workspace` or a read-write memory root; the request is rejected instead of relying on path precedence. Path normalization, archive traversal checks, symlink rejection, and collision checks run before bootstrap. Other backends retain the broader public absolute-path contract; this E2B restriction is a documented runtime difference.

Only the create path uploads files. Resume verifies the existing seal and contents but does not repair, replace, delete, or re-upload anything. If verification fails, the turn fails closed and a new Session is required.

Files changed under `/workspace` and read-write memory roots survive later turns while the same E2B sandbox remains resumable. They are not automatically exported to S3-compatible storage. In particular, edits to a read-write Memory Store seed are **not** written back to VMA's managed Memory Store records or versions.

R2 or another S3-compatible store remains the durable source used to resolve file and Skill content at Session creation. It is not continuously synchronized with the sandbox. VMA does not currently implement managed-file synchronization or automatic generated-artifact export.

### Seal boundary

The current bootstrap protects control-plane-owned immutable files with root ownership, non-writable permissions, protected parent directories, and digest verification before every turn. This is a narrow guarantee: it does not prove that the guest can write only to `/workspace` and approved read-write memory roots, and it does not turn all other template files into immutable mounts.

The required hardened E2B template must guarantee that tenant code cannot write outside approved mutable roots. It should remove unnecessary capabilities and privilege-escalation paths, harden every non-mutable directory, and enforce process and filesystem limits independently of VMA's manifest. VMA checks only the configured non-root default user and absence of passwordless sudo; it does not scan or harden the template's whole Linux filesystem. The current seal protects VMA-owned immutable inputs, not the entire guest filesystem.

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
8. Workloads do not rely on generated files or Memory Store edits being exported from the sandbox.
9. Logs, traces, previews, and errors redact secrets and external provider identifiers.
10. Provider outages produce visible, retryable errors rather than silent reseeding or replacement.

Until those checks pass, VMA's Environment resource is a compatibility/control-plane object rather than Claude-equivalent managed infrastructure.
