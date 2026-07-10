# Sandbox Runtime

VMA maps a Claude Managed Agents `Environment` onto a Deep Agents backend. The sandbox is the security boundary for model-controlled code. A tool permission prompt, filesystem path check, container working directory, or model instruction is not a substitute for process isolation.

Official compatibility targets:

- Cloud environments: https://platform.claude.com/docs/en/managed-agents/environments
- Cloud sandbox reference: https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference
- Self-hosted sandboxes: https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes

Current status is **partial**. VMA defines the backend integration point and a safe no-shell default, but does not ship a production remote container or VM provider.

## Backend selection

`app.runtime.sandbox.open_backend()` selects one of three paths:

| Condition | Deep Agents backend | Shell execution | Policy enforcement |
| --- | --- | --- | --- |
| `VMA_SANDBOX_FACTORY` is configured | Operator-provided backend | Depends on backend; production factories should implement `SandboxBackendProtocol` | Factory/provider responsibility |
| Environment type is `local` and `VMA_ALLOW_UNSAFE_LOCAL_SANDBOX=true` | `LocalShellBackend` | Yes, on the control-plane host | No |
| Otherwise | `StateBackend` | No | No remote isolation; file state only |

The safe default is `StateBackend`. It gives Deep Agents a virtual filesystem stored in LangGraph state. With a durable checkpointer, that state can survive turns on the same thread. It does not run commands, install packages, enforce network policy, or provide a container.

Environment fields such as `networking`, `packages`, `resources`, and `sandbox.backend` are validated and included in the session's sandbox summary. They do not select or configure a real provider by themselves. A remote factory must consume and enforce them.

## Production factory contract

Configure a Python callable using `module:attribute` syntax:

```dotenv
VMA_SANDBOX_FACTORY=my_service.sandboxes:create_backend
```

VMA calls it with keyword arguments:

```python
def create_backend(
    *,
    workspace_id: str,
    session_id: str,
    environment_config: dict,
):
    ...
```

The return value may be:

- A Deep Agents `BackendProtocol` instance.
- A `SandboxBackendProtocol` instance when `execute` should be available.
- An awaitable resolving to a backend.
- An async context manager yielding a backend.

If the returned object exposes `aclose()`, VMA calls it after the run. A provider that must preserve one sandbox across multiple session turns should treat `workspace_id` plus `session_id` as the stable lookup key and make open/close idempotent. It must not assume the Python backend object itself remains alive between requests or workers.

Deep Agents provides `BaseSandbox` as a useful base for remote adapters. Its default filesystem operations execute commands inside the remote environment and assume a POSIX-like shell, common command-line tools, and `python3`. A provider may instead implement native upload, download, file, and execute methods.

## Required isolation properties

A production factory should enforce at least:

- A dedicated container, microVM, VM, or equivalently strong boundary per session or approved isolation scope.
- A filesystem root that cannot traverse into the control-plane host or another tenant.
- CPU, memory, process-count, disk, output-size, and wall-clock limits.
- Command cancellation and hard termination after timeout.
- Network deny-by-default or an enforceable allowlist matching the environment config.
- Explicit exceptions for approved MCP endpoints and package registries.
- No inherited control-plane environment, cloud metadata credentials, database credentials, or provider API keys.
- Short-lived, least-privilege secret injection only for tools that need it.
- Safe archive extraction, symlink handling, upload limits, and read-only mounts.
- Tenant-scoped logs and artifacts with secret redaction.
- Idempotent create, reconnect, stop, snapshot, and delete operations.
- Cleanup after expiry and a recovery path when the control-plane process dies mid-run.

The provider should return an opaque sandbox ID for operator diagnostics without exposing a cross-tenant lookup capability to public clients.

## Environment policy mapping

VMA currently recognizes these policy groups:

```json
{
  "type": "cloud",
  "networking": {
    "type": "limited",
    "allowed_hosts": ["api.example.com"],
    "allow_mcp_servers": true,
    "allow_package_managers": false
  },
  "packages": {
    "apt": [],
    "cargo": [],
    "gem": [],
    "go": [],
    "npm": [],
    "pip": []
  },
  "resources": {
    "cpu": 2,
    "memory_mb": 4096,
    "disk_mb": 10240,
    "timeout_seconds": 900
  }
}
```

The control plane normalizes and reports this data. Only the remote sandbox provider can make it true. A factory must fail closed when it cannot enforce a requested restriction; it must not silently label an unrestricted environment as restricted.

## Files, skills, memory, and artifacts

The runtime can prepare session file bytes, custom skill archives, and memory context before graph execution. A complete remote provider must define where those inputs live:

- Uploaded files should appear at the requested absolute mount path and honor read-only flags.
- Custom skill directories must remain inside the tenant sandbox and must not escape through archive symlinks or traversal.
- Generated files should be copied back to S3-compatible object storage before sandbox deletion and represented as public session resources/events.
- Memory-store records currently enter the model as bounded context; they are not a bidirectional filesystem mount yet.
- Deep Agents may offload long conversation or tool output into backend paths such as `/artifacts`. The provider must include those paths in quota and artifact policy.

These semantics are still partial; see [known incompatibilities](./known-incompatibilities.md#files-skills-and-memory).

## Tool permissions are not containment

Deep Agents filesystem permissions apply to its built-in file tools. Direct backend calls bypass that middleware, and shell commands can access files without going through `read_file` or `write_file`. Deep Agents 0.6.12 does not generally combine path permissions with an execution-capable backend because `execute` can bypass them.

Therefore:

- Use human approval to control user intent and workflow.
- Use backend/sandbox policy to control actual authority.
- Treat `execute` as arbitrary code execution inside the sandbox.
- Apply independent policy to MCP and HTTP tools, which do not run through filesystem middleware.

## Unsafe local mode

Local execution is opt-in:

```dotenv
VMA_ALLOW_UNSAFE_LOCAL_SANDBOX=true
VMA_SANDBOX_ROOT=/workspace
```

For a `local` environment, VMA derives a workspace/session-specific directory, enables `LocalShellBackend` virtual path handling, disables general environment inheritance, and supplies a minimal `PATH`.

This is still host shell execution. `virtual_mode` confines Deep Agents file helpers but does not turn the subprocess into a container or enforce network/resource policy. Do not enable this mode in a shared API process, CI runner with secrets, developer laptop exposed to untrusted input, or any multi-tenant deployment.

## Self-hosted work is not a sandbox

The optional `vma-worker` and environment work routes provide queue/lease mechanics for `self_hosted` environments. They do not create an isolated execution environment. A worker that claims work must still open an approved sandbox backend and enforce the environment policy.

The current worker protocol is partial: lease and heartbeat behavior exists, but strong distributed fencing, worker identity/RBAC, sandbox attestation, and durable cancellation remain production gaps. See [work queue](./work-queue.md).

## Operational readiness checklist

Before enabling `execute` for tenants, verify:

1. `VMA_SANDBOX_FACTORY` returns an isolated remote backend.
2. Cross-workspace and cross-session filesystem tests fail closed.
3. Egress and metadata-service blocking are enforced outside the guest.
4. Provider/model credentials never enter the guest unless explicitly required.
5. Run timeout cancels and then forcibly terminates remote commands.
6. Session deletion eventually deletes or irreversibly detaches its sandbox.
7. Artifacts are uploaded before teardown and cannot overwrite another workspace's keys.
8. Logs, traces, previews, and errors redact secrets.
9. A worker crash can be recovered without creating two active sandboxes for one run.
10. Provider outages produce retryable session events rather than silent loss.

Until those checks pass, VMA's environment resource is a compatibility/control-plane object rather than Claude-equivalent managed infrastructure.
