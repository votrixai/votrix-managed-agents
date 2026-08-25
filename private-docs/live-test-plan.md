# Live tests: running the whole thing for real

Internal only. This is the plan for `tests_live/` — the suite that exercises a
real Postgres, a real bucket, a real E2B sandbox and a real model, end to end
through the HTTP API. The fast suite under `tests/` stays what it is: seconds,
no network, no money.

Snapshot: 2026-07-29.

## Why a second suite at all

`tests/conftest.py` replaces the database with SQLite and stubs the bucket and
the sandbox. A live test needs the opposite of every one of those, and sharing
that conftest means fighting it in each fixture. So the two live apart and are
run apart:

```
pytest tests/         seconds, on every change
pytest tests_live/    ~20 minutes, by hand
```

`testpaths = ["tests"]`, so a bare `pytest` never picks these up.

## Determinism without a scripted model

The obvious way to make a test that involves an LLM repeatable is to script the
model. It was the first plan here and it was wrong: a scripted model tests the
translation layer against a fiction, and the thing most worth knowing — whether
a real model actually emits five parallel tool calls when told to — is exactly
what gets stubbed out.

Everything runs on a real model instead, steered two ways.

**The agent's system prompt makes obedience the job:**

> You are an execution assistant. Do what the user asks, no more and no less.
> When asked to do several things at once, send every tool call together in one
> reply rather than one at a time.

**Every user message names the tools and their arguments.** Not "analyse the
data" but "read these four files: …". The test says what it needs; it does not
hope for it.

Where a model still declines to batch, the test fails — and that failure is
worth having. The multi-answer design rests on models issuing parallel calls;
if one does not, that path is unreachable in production too, and a green test
would be hiding it.

## The one thing that is faked

`web_search`, pinned to a fixed result. A live search returns something new
every time and there is nothing to assert against. Everything else — the
sandbox, the skill unpack, the file round trip, the model — is real.

Images are out of scope: `user.message` carries text blocks only. Files reach
the agent by being mounted into the sandbox at session start, which is a
different thing from multimodal input and is what these tests cover.

---

## What the suite is actually about

Three kinds of tool exist at runtime, and every interesting test is a statement
about how they mix:

| kind | example | `interrupt_on` entry | how it completes |
| --- | --- | --- | --- |
| direct + allow | `read_file`, `ls`, `glob` | none | runs in the sandbox |
| direct + ask | `execute`, `task` | `["approve", "reject"]` | `user.tool_confirmation` |
| custom | `get_crm_record`, `ask_user` | `["respond"]` | `user.custom_tool_result` |

There is no fourth kind. A custom tool is *always* an interrupt and *always*
respond-only — `engine.py` overwrites whatever the toolset config said, because
a custom tool has no implementation on our side and the client's reply is the
only way one ever completes. Asking for permission first and then asking for
the result would be asking twice.

That table gives the suite its shape. A single model reply can mix all three,
and three facts about the mixture are what these tests are for:

1. **A batch stops as a batch.** The graph pauses in `after_model`, which is a
   node *before* `tools`. So a call that needed no permission is waiting too,
   and its result does not appear until the ones that did are answered.
2. **Decisions are matched by position.** `HumanInTheLoopMiddleware` walks the
   list it was handed and pairs it with its own request index by index. Filter
   the pending list wrong by one and an approval lands on a different call —
   **with nothing anywhere reporting it**. This is the only silent failure in
   the system, and scenarios 22–24 exist for it alone.
3. **The client may answer in any order.** Each answer names its call, so
   `_build_resume` reorders. Scenarios that answer in order prove nothing;
   the ones that scramble do.

---

## Layout

```
tests_live/
├── conftest.py             real fixtures
├── helpers.py              running a turn, reading it back
├── stubs.py                the pinned web_search
├── fixtures/
│   ├── skill/revenue-report/SKILL.md
│   └── uploads/  revenue.csv  notes.txt  config.json  readme.md
├── test_basics.py          1–6b
├── test_confirm_single.py  7–12b
├── test_confirm_batch.py   13–26
├── test_lifecycle.py       27–32, plus 28b
├── test_sandbox.py         36–44
├── test_delegation.py      45–49
└── test_stream.py          33–35
```

### Fixtures

Built once for the whole run, because they are the expensive part:

| Fixture | Cost | Why once |
| --- | --- | --- |
| `server` | — | uvicorn **in-process**: the `web_search` stub has to reach the same interpreter, and SSE needs a real socket rather than an ASGI transport |
| `organization` | — | one tenant row, written directly; Organization creation is intentionally absent from the public API |
| `environment` | ~0s | **no packages**, so it runs on the base image and there is no build to wait for |
| `skill` | ~2s | upload and unpack once |
| `uploads` | ~2s | the four files, uploaded once |
| `agent` | — | tools, permissions and the system prompt above |

Per test:

| Fixture | Cost |
| --- | --- |
| `session` | ~21s — a fresh sandbox with the four files mounted |

A few scenarios deliberately continue an existing session rather than paying
for a new sandbox to prove a point about memory.

### Dispatch is inline, and that is what makes this readable

`TURN_DISPATCH` defaults to `inline`, so `POST /v1/sessions/{id}/events` does
not return until the turn is over. There is no polling anywhere in this suite:

```
before = session.last_event_seq
POST   /v1/sessions/{id}/events        blocks for the whole turn
GET    /v1/sessions/{id}/events?after_seq=before      the turn, exactly
```

The two tests that need a second request *during* a turn (interrupt, live file
capture) fire the first one as a task and hold it.

### Agent configuration

Tool names are Deep Agents' own: `execute`, `ls`, `read_file`, `write_file`,
`edit_file`, `glob`, `grep`, `write_todos`, `task`.

```
agent_toolset_20260401     execute = always_ask
                           task    = always_ask
                           rest    = always_allow
web_toolset_20260401       web_search pinned by the stub
custom tools               get_crm_record, ask_user
```

`task` is set to ask for a reason that has nothing to do with danger: it is the
sub-agent entry point, and work delegated through it happens in a thread we do
not stream. If a model reaches for it, the test should stop and say so rather
than lose two minutes of activity into a gap.

### Helpers

Nearly all the difficulty in a live test is arranging the round trip. Four
functions carry it:

```
run_turn(session_id, text)         send a message, return the turn's events
answer(session_id, answers)        reply to a batch in any order, return the
                                   events that followed
pending(events)                    the tool_use_ids the last idle asked for
kinds(events)                      ["agent.thinking", "agent.tool_use", …]
```

With those, a scenario is three or four lines.

---

## The scenarios

### Basics — `test_basics.py`

| # | What the agent is told | What is asserted |
| --- | --- | --- |
| 1 | Use the revenue-report skill on `uploads/revenue.csv`, write the answer to `outputs/report.txt` | the skill really unpacked, the output was collected, downloading it returns the real bytes, in the format only the uploaded SKILL.md describes |
| 2 | Read these four files at the same time | one reply, four `agent.tool_use`, all `evaluated_permission: allow`, four results, **no** `requires_action` |
| 3 | Search the web for one term | the pinned result comes back as `agent.tool_result` |
| 4 | (second turn) "What number did you just calculate?" | the answer proves the **checkpoint** carries the history, not the event log |
| 5 | any turn | `agent.thinking`, if it appears at all, is non-empty — see *loose assertions* below |
| 6 | any turn | the `agent.message` texts concatenate to the model's whole reply: nothing dropped, nothing repeated |

### One pending call — `test_confirm_single.py`

| # | Scenario | What is asserted |
| --- | --- | --- |
| 7 | `execute` → approve | the command really ran; `agent.tool_result` holds its real output |
| 8 | `execute` → reject with a reason | `is_error`, the reason reached the model, the model takes another route |
| 9 | custom `get_crm_record` → respond | the model's summary contains what we answered with |
| 10 | custom → respond `is_error: true` | the result comes back **flagged as an error**, not as a successful call that returned the sentence "the CRM is unavailable" |
| 10b | custom → respond, no flag | the control: nothing is an error by accident |
| 11 | **send `tool_confirmation` for a custom call** | `approve` is not in that call's `allowed_decisions` → the turn fails cleanly, `session.error` explains it, `session.status_idle` with `stop_reason.type == "error"` ends it, and the session is still usable |
| 12 | **send `custom_tool_result` for a direct ask call** | the mirror image, equally clean |
| 12b | answer when nothing is pending | there is no interrupt to resume; same clean failure |

### A batch of pending calls — `test_confirm_batch.py`

The centre of the suite. Every row is one model reply containing several calls.

**One kind at a time**

| # | The calls | The answers | The point |
| --- | --- | --- | --- |
| 13 | ask ×3 | all approve | three commands, three distinct outputs |
| 14 | ask ×3 | two approve, one reject | half a batch succeeds and half fails |
| 15 | custom ×3 | in the order asked | the plain case, first |
| 16 | custom ×3 | **reversed** | the reordering is real, not a coincidence |

**Mixed kinds** — the gap the old plan left

| # | The calls, in the order the model sends them | The answers | The point |
| --- | --- | --- | --- |
| 17 | `[allow, ask]` | approve | **the allow call is waiting too** — its result appears only after the ask is answered |
| 18 | `[allow, custom]` | respond | the same fact with a custom call |
| 19 | `[ask, custom]` | approve + a result | **two different event types answering one pause, in one request** |
| 20 | `[custom, ask]` | reversed: the ask first, then the custom; reject + a result | order reversed and polarity mixed at once |
| 21 | `[ask, custom]` | approve + `is_error` | the other diagonal |
| 22 | `[allow, ask, custom]` | all | the allow call is filtered out of the pending list **from the front** |
| 23 | `[ask, allow, custom]` | all | filtered out **from the middle** |
| 24 | `[ask, custom, allow]` | all | filtered out **from the end** |

22–24 are the same statement at three positions. They are not redundant: the
middleware pairs decisions with its request by index, so dropping the wrong
element shifts every decision after it, and the resulting mismatch is invisible
— the approval simply lands on the wrong tool and everything reports success.

**Incomplete and over-complete answers**

| # | Scenario | What is asserted |
| --- | --- | --- |
| 25 | three pending, two answered | the short list goes to the graph as it is; LangGraph's own `ValueError` reaches the client as a `session.error`, the turn ends with `stop_reason.type == "error"`, and the session is still usable |
| 26 | three pending, four answers (one unknown id) | the extra is ignored — it is not in the pending list, so it never becomes a decision — and the other three resolve normally |

### Lifecycle — `test_lifecycle.py`

| # | Scenario | What is asserted |
| --- | --- | --- |
| 27 | pending calls, and instead of answering, send a new message | every pending call is cancelled and the model moves on |
| 28 | interrupt a running turn | idle with `stop_reason.type == "interrupted"`, nothing written afterwards, and the next message continues the conversation |
| 29 | a call announced on one turn, resolved on the next | **the same `tool_use_id` string** appears in `agent.tool_use`, in `stop_reason.tool_use_ids`, in the client's answer, and in `agent.tool_result` — four places, one string, no translation |
| 30 | `POST /live/files` while the agent is still working | the returned id downloads real bytes, and `storage_key` appears nowhere in the response |
| 31 | a second message while the turn is running | 409 `session_busy`, and **nothing** was appended to the event log |
| 32 | the container is gone, then a message | `session.error` says `sandbox_unavailable`, `session.status_terminated` ends it, and every later message is refused |

### The sequence race — `test_lifecycle.py`

| # | Scenario | What is asserted |
| --- | --- | --- |
| 28b | eight appends to one session at once, each on its own database session | every one gets a distinct `seq`, and the log reads 1…8 with no gaps |

Scenario 28 found this the hard way and is a poor regression test for it,
being timing-dependent. This provokes it directly and in seconds: no sandbox,
no model, just the shape of a worker emitting output while an interrupt
arrives. Eight and not eighty — each needs a connection and the pooler hands
out fifteen in total.

### The container doing real work — `test_sandbox.py`

Everything in the confirmation suites runs `echo`. These do not.

| # | Scenario | What is asserted |
| --- | --- | --- |
| 36 | `execute` runs a `python3` one-liner over the CSV | the tool result contains `PYSUM 440` — a number the container computed, not one the model could invent |
| 37 | `execute` runs `wc -l` on an uploaded file | the real line count |
| 38 | write to `outputs/reports/2031/q1.txt` | collected, and the subdirectory survives into the filename |
| **39** | **write to `/home/user/scratch_note.txt`** | the agent reads it back in the same turn, so it exists — and it is **never collected**. The boundary, stated |
| 40 | three files in one turn | all three collected and downloadable |
| 41 | uploads → read → upper-case → `outputs/` → download | one file's bytes make every hop |
| 42 | `ls` | the four mounted filenames |
| 43 | `glob` and `grep` | both find `notes.txt` |
| 44 | `write_file` then `edit_file` | the download shows the edit and not the original |

### Planning and delegation — `test_delegation.py`

These need `planning_agent`, which is the same agent without the prompt line
forbidding `task` and `write_todos`. The strict agent keeps that line because
a stray `write_todos` turns a three-call batch into a four-call one, and
scenarios 22–24 are counting.

| # | Scenario | What is asserted |
| --- | --- | --- |
| 45 | `ask_user` | the second custom tool, never previously called |
| 46 | `write_todos` | `allow`, resolves, does not pause |
| **47** | **`task`, approved** | the *other* `always_ask` tool, and the only one that starts a second agent. Also asserts the sub-agent's own `read_file` **never reaches our stream** — a gap recorded rather than fixed. If sub-agent events are ever surfaced, this is what starts failing |
| 48 | `task`, refused | the agent does the work itself, where we can see it |
| 49 | ask for a URL to be fetched | `web_fetch` is **not offered**, and the turn ends normally. It used to be installed with a body that raises, and the model really did reach for it |

### The stream — `test_stream.py`

| # | Scenario | What is asserted |
| --- | --- | --- |
| 33 | one SSE connection across a whole turn | events arrive one at a time, in `seq` order, and match what `GET /events` returns |
| 34 | drop it and reconnect with `Last-Event-ID` | nothing lost, nothing repeated |
| 35 | a turn that pauses, watched from the stream | `session.status_idle` carrying `requires_action` arrives on the stream, so a client never has to poll to learn it is being asked something |

---

## What the first full run taught us

38 scenarios, 53:55, and everything about the application passed. The two
things worth writing down came from watching it rather than from an assertion.

**An interrupt is not immediate, and cannot be.** Scenario 28 is much slower
than its neighbours. Cancellation takes effect when the worker's *next* write
is refused, and with `stream_mode="values"` a write only happens between graph
nodes — so nothing is refused until the model has finished generating whatever
it was in the middle of. Interrupting a thousand-word answer means waiting for
that answer. The behaviour is correct (the stop reason is right, nothing is
written afterwards, the conversation continues), but "how fast is an interrupt"
is a question about the model, not about us.

**The suite earns its keep on configuration, not just on logic.** The only
failures in the run came from switching `pool_pre_ping` off — a change made on
a benchmark that had been read wrong. Thirty `connection is closed` errors,
all of them in `test_stream.py`, because SSE polls in a loop and its
connections sit idle longest. No unit test could have found that; nothing but
a real database over a real network behaves that way.

## Assertions that have to stay loose

`agent.thinking` carries the model's reasoning, and not every model returns
any — Anthropic only emits it with extended thinking on, which we do not turn
on. The assertion is conditional: if the event appears its content must be
non-empty, and if none appears that is recorded rather than failed. What is
being tested is our rule — emit when there is something, never emit an empty
one — not whether a particular provider exposes reasoning.

The same applies to what a model *says*. Assertions are about events, ids,
ordering and file bytes. Where a test has to look at prose it looks for a
token the tool result put there (a customer id, a number the CSV implies),
never for a phrasing.

## Housekeeping

Each test kills its session's container and deletes the session. Killing is
separate on purpose: `DELETE /v1/sessions/{id}` only sets `deleted_at`, and a
container left alone bills until E2B's own idle timeout.

The run deletes the skill and the uploaded files at the end, and *archives* the
environment rather than deleting it — sessions are soft-deleted, so their rows
still reference it and the foreign key refuses. That is correct behaviour, not
something the suite should route around.

Keys come from `.env` (`ANTHROPIC_API_KEY`, `E2B_API_KEY`, the `S3_*` group,
`DATABASE_URL`). A missing one skips the suite naming what is missing, rather
than failing somewhere deep inside a provisioning call.

## Cost, and where it goes

Measured rather than estimated. The reference scenario is `test_6b` — one turn
that calls no tools at all, so it is the floor everything else is built on.

| | |
| --- | --- |
| session fixture (one sandbox) | 22–53s |
| a turn with no tools at all | ~18s |
| each extra tool round trip | ~15s |
| image build | none — the environment declares no packages |
| whole run, 38 scenarios | **~57 minutes** |

The instrumentation added in `app/utils/timing.py` is what makes the rest of
this section facts rather than suspicions.

### The floor, itemised

`test_6b` end to end, before and after connection pooling was switched on:

| | before | after |
| --- | ---: | ---: |
| `database_connection_acquired` (24×) | 41.9s | **5.1s** |
| `database_statement` (64×) | 35.6s | 36.3s |
| `sandbox_connected reused=True` (9×) | 10.0s | 10.1s |
| checkpoint calls (17×) | 8.0s | 9.4s |
| `sandbox_command` (8×) | 5.3s | 6.4s |
| **whole test** | **131.6s** | **89.7s** |

### The database was never doing any work

Against the Supabase pooler in us-east-2, from here:

```
TCP handshake                    224 ms      one round trip ≈ 150 ms
full connect (TCP + TLS + auth) 1872 ms
SELECT 1                         402 ms
SELECT count(*) FROM pg_class    402 ms      ← identical
```

A trivial query and a full table count cost the same, so none of it is
execution — it is all distance. That rules out indexes and query shape as
levers, and leaves only *how many round trips* the service makes.

`private-docs/db-latency-probe.py` is a standalone version of that measurement,
for running from somewhere else to find out how much of this is geography.

### What is left, in order

1. **`database_statement`, 36s.** `_connect_args` disables the prepared
   statement cache, which is required against a pgbouncer-style pooler in
   *transaction* mode; this deployment is on 5432, which is *session* mode.
   Measured, enabling it takes a repeated query from 402ms to 215ms.
2. **`sandbox_connected reused=True`, 10s.** `ensure_connected` costs *more*
   on the branch meant to be cheap — `is_running()` plus `set_timeout()` is
   two round trips, where reconnecting outright is one.
3. **`checkpoint_setup`, ~1s every turn**, for a migration check that has
   nothing to do after the first.
4. **`outputs_discovered`, ~1.2s every turn**, to find an empty directory.
