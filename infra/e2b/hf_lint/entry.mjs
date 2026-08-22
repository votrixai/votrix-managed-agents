/**
 * The HyperFrames composition linter, as one command that reads a directory
 * and writes one JSON object.
 *
 * This exists because the linter is the only part of HyperFrames we cannot
 * write ourselves: its rules are the executable form of what HeyGen's renderer
 * accepts, and a render that fails those rules is still billed. Everything
 * else the CLI does we either already have or do not want, so the whole 385 MB
 * package is reduced here to `lintProject` and bundled to a single file.
 *
 * Contract with the Python caller:
 *   stdout    one JSON object, always, when the exit code is 0
 *   stderr    a stack trace, when the exit code is not 0
 *   exit 0    the lint ran — findings may still be present
 *   exit 2    the lint could not run
 *
 * Finding paths are made relative to the project directory. The caller lints
 * inside a temporary directory whose name is meaningless to anyone reading the
 * findings, and it should not travel back to the agent.
 */
import { relative, isAbsolute } from "node:path";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { lintProject, LINT_RULE_COUNT } from "@hyperframes/lint";

// The smallest composition that passes: a root with dimensions, a duration,
// and a registered timeline. `--selftest` lints it during the image build, so
// a broken bundle fails the build instead of the first render.
const SELFTEST_HTML = `<!doctype html><html><head><script>
window.__timelines = window.__timelines || {};
window.__timelines["main"] = { paused: true };
</script></head><body>
<div data-composition-id="main" data-start="0" data-duration="5"
     data-width="1920" data-height="1080"></div>
</body></html>`;

async function report(projectDir) {
  const result = await lintProject(projectDir);
  return {
    rule_count: LINT_RULE_COUNT,
    errors: result.totalErrors,
    warnings: result.totalWarnings,
    infos: result.totalInfos,
    findings: result.results.flatMap((entry) =>
      entry.result.findings.map((finding) => {
        const raw = finding.file ?? entry.file;
        return {
          ...finding,
          file: isAbsolute(raw) ? relative(projectDir, raw) || "index.html" : raw,
        };
      }),
    ),
  };
}

async function selftest() {
  const dir = mkdtempSync(join(tmpdir(), "hf-selftest-"));
  try {
    writeFileSync(join(dir, "index.html"), SELFTEST_HTML);
    const out = await report(dir);
    if (out.rule_count < 1) throw new Error("linter reported no rules");
    if (out.errors > 0) {
      throw new Error(`minimal composition failed: ${JSON.stringify(out.findings)}`);
    }
    process.stdout.write(`selftest ok — ${out.rule_count} rules\n`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function main() {
  const arg = process.argv[2];
  if (arg === "--selftest") return selftest();
  if (!arg) throw new Error("usage: lint.cjs <project-dir> | --selftest");
  process.stdout.write(JSON.stringify(await report(arg)));
}

main().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(2);
});
