"""The image `/v1/sandbox` runs HyperFrames composition checks in.

A composition is HTML that HeyGen renders to video, and HeyGen bills for
renders it can technically produce: a composition referencing a missing asset
renders fine and returns a video with a hole in it, still charged. The rules
that catch that are the executable form of what its renderer accepts, so they
are `@hyperframes/lint` itself rather than a reimplementation — a check that
disagrees with the renderer is worse than no check, because it is trusted.

Two decisions here are worth stating.

**The version is exact, with no caret.** Upstream publishes about twice a day;
measured, the rule count moved from 81 to 79 inside three days. A range would
mean the check quietly becoming a different check between one container and
the next.

**The rules are bundled to one file and `node_modules` is deleted.** Resolving
299 modules off disk costs about 1.4 seconds every run; the bundle loads in
under a second and leaves 1.4 MB in the image instead of 1,812 files.

The build fails if the rule count is not what this file says. That is the one
protection against the pin above being edited without anyone noticing what it
changes.
"""

import os
import sys

from e2b import Template

TEMPLATE_NAME = "votrix-hf-lint"
TEMPLATE_CANDIDATE = f"{TEMPLATE_NAME}:v20260821-2"
TEMPLATE_CPU_COUNT = 1
TEMPLATE_MEMORY_MB = 1024

LINT_VERSION = "0.8.5"
EXPECTED_RULE_COUNT = 81
ESBUILD_VERSION = "0.25.12"

INSTALL_DIR = "/opt/hflint"
LINT_ENTRYPOINT = f"{INSTALL_DIR}/lint.cjs"

_BUILD = f"""
set -eux
cd {INSTALL_DIR}
npm install @hyperframes/lint@{LINT_VERSION} --no-audit --no-fund
npm install esbuild@{ESBUILD_VERSION} --no-audit --no-fund
node -e "const l=require('{INSTALL_DIR}/node_modules/@hyperframes/lint');
if (l.LINT_RULE_COUNT !== {EXPECTED_RULE_COUNT}) {{
  throw new Error('rule count is '+l.LINT_RULE_COUNT+', expected {EXPECTED_RULE_COUNT}');
}}
console.log('rules', l.LINT_RULE_COUNT);"
npx esbuild entry.mjs --bundle --platform=node --format=cjs --outfile={LINT_ENTRYPOINT}
rm -rf {INSTALL_DIR}/node_modules {INSTALL_DIR}/package.json \
       {INSTALL_DIR}/package-lock.json {INSTALL_DIR}/entry.mjs
node {LINT_ENTRYPOINT} --selftest
"""

template = (
    Template()
    .from_node_image("22")
    .set_user("root")
    .run_cmd(f"mkdir -p {INSTALL_DIR}", user="root")
    .set_workdir(INSTALL_DIR)
    # Relative to the build context, which is this directory.
    .copy("entry.mjs", f"{INSTALL_DIR}/entry.mjs", user="root")
    .run_cmd(_BUILD, user="root")
    .set_workdir("/home/user")
    .set_user("user")
)


def main() -> None:
    if not os.environ.get("E2B_API_KEY"):
        sys.exit("E2B_API_KEY is required")
    from e2b import default_build_logger

    info = Template.build(
        template,
        TEMPLATE_CANDIDATE,
        cpu_count=TEMPLATE_CPU_COUNT,
        memory_mb=TEMPLATE_MEMORY_MB,
        on_build_logs=default_build_logger(),
    )
    print(f"built template_id={info.template_id} build_id={info.build_id}")


if __name__ == "__main__":
    main()
