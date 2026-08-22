# votrix-hf-lint

The image `/v1/sandbox` runs HyperFrames composition checks in. It holds the
`@hyperframes/lint` rules, bundled to one file, and nothing else.

Callers reach it by name rather than by environment id:

```json
POST /v1/sandbox/create
{"system_environment": "hf-lint", "ttl_seconds": 120}
```

## Build and promote

Run from this directory — `copy()` resolves against the build context, which
is the working directory. The ignored local `.env` at the repository root must
contain `E2B_API_KEY`; nothing here prints it.

```bash
cd infra/e2b/hf_lint
set -a && . ../../../.env && set +a
../../../.venv/bin/python template.py
```

That builds the versioned candidate in `TEMPLATE_CANDIDATE`. Move the
unversioned tag only once the candidate is proven, because
`SYSTEM_ENVIRONMENTS` points at the unversioned name and every new container
resolves it:

```bash
../../../.venv/bin/python -c \
  'from e2b import Template; Template.assign_tags("votrix-hf-lint:v20260821-2", "default")'
```

## The pin

`LINT_VERSION` is exact and `EXPECTED_RULE_COUNT` is asserted during the
build. Upstream publishes about twice a day and the rule count has moved
inside a three-day window, so a version range would mean the check quietly
becoming a different check between one container and the next.

Bumping the version means bumping the rule count in the same commit, and the
build is what tells you the new number: it fails and prints what it found.
