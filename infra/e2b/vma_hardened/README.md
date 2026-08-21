# VMA hardened E2B template

This template is the trusted guest image for one persistent E2B sandbox per
VMA Session. Deep Agents, provider SDKs, and all API keys stay in the VMA
control plane; they are not installed or copied into this image.

The guest runs as the non-root `user`, without passwordless sudo, from
`/workspace`. VMA owns `/mnt/session`, `/skills/custom`, read-only memory roots,
and `/var/lib/vma`. It creates individual read-write memory roots during the
one-time Session bootstrap. `/tmp` and `/var/tmp` intentionally remain writable
sticky, session-local scratch paths for tool compatibility. E2B also recreates
provider-managed writable paths such as `/usr/local`, `/code`, and `/home/user`
when a sandbox starts. VMA does not treat those paths as trusted: every root
bootstrap runs through isolated `/usr/bin/python3 -I -S`, while durable tenant
work remains under `/workspace` or a read-write memory root.

## Build, test, and promote

Run from the repository root. The ignored local `.env` must contain
`E2B_API_KEY`; none of these commands print it.

```bash
uv sync --extra sandbox-e2b
uv run --extra sandbox-e2b --env-file .env python infra/e2b/vma_hardened/build.py
uv run --extra sandbox-e2b --env-file .env python -c 'from e2b import Template; print(Template.get_tags("vma-hardened"))'
uv run --extra sandbox-e2b --env-file .env python infra/e2b/vma_hardened/smoke.py
uv run --extra sandbox-e2b --env-file .env python infra/e2b/vma_hardened/provider_smoke.py
uv run --extra sandbox-e2b --env-file .env python -c 'import sys; sys.path.insert(0, "infra/e2b/vma_hardened"); from e2b import Template; from template import TEMPLATE_CANDIDATE; Template.assign_tags(TEMPLATE_CANDIDATE, "default")'
uv run --extra sandbox-e2b --env-file .env python -c 'from e2b import Template; print(Template.get_tags("vma-hardened"))'
uv run --extra sandbox-e2b --env-file .env python infra/e2b/vma_hardened/provider_smoke.py --template vma-hardened
```

Never publish this template. It should remain private to the E2B team that owns
the server-side VMA API key. Build a new version tag and pass the smoke test
before moving `default` again.

**Read the promoted version from `template.py`, never type it.** This step used
to carry a hand-written literal, and it drifted: it named a build from the day
before `zip` joined the package list, so `default` kept pointing at a sandbox
without it long after the code had it. Agent sessions failed on `zip -r` with
`command not found`, which looks like a broken product, not a broken build. The
final `provider_smoke.py --template vma-hardened` line above is what catches a
stale `default` — it now asserts the agent toolchain against a real sandbox
built from the promoted tag, which is the only check that can see this. Never
skip it after promoting.

VMA must use the unversioned default and declare the matching resources:

```dotenv
VMA_E2B_TEMPLATE=vma-hardened
VMA_E2B_GUEST_USER=user
VMA_E2B_WORKDIR=/workspace
VMA_E2B_TEMPLATE_RESOURCES={"cpu":2,"memory_mb":2048}
```

Do not declare `disk_mb`: E2B disk capacity is plan-defined rather than a
template build input, and VMA deliberately rejects a per-Environment disk claim
that it cannot attest.
