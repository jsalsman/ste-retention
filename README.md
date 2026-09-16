# STE Retention

This project answers one question. Does a model follow a writing standard
better when you give the standard a name?

It also provides a small Flask research interface by Jim Salsman. The interface
supports one-off model interaction, bounded on-demand previews, and safe viewing
of persisted results. It does **not** silently run a study during a page request.

## Architecture and repository layout

- `flask-app.py` exposes `app` for Gunicorn and owns HTTP validation, errors,
  request context, and NDJSON serialization.
- `experiment.py` validates strict interactive limits and emits transport-neutral
  progress dictionaries. `openrouter.py` accepts the API key explicitly.
- `scoring.py`, `statistics_utils.py`, `records.py`, and `leaderboard.py` isolate
  scoring, statistics, JSONL persistence, and escaped HTML rendering.
- `run_store.py` writes credential-free per-run snapshots and validates resume
  identifiers and persisted schema before any additional paid work.
- `ste_retention.py` and `make_leaderboard.py` are thin command-line entry points.
- Root `index.html` is standalone. `static/styles.css` and `static/app.js` provide
  presentation and behavior; no `templates/` directory is used.

The JSONL record shape remains compatible with the original unversioned format:
each object contains `session`, `model`, `variant`, `depth`, `score`, optional
`metrics`, and response `text`. Readers report malformed input with its line
number. If a future schema changes these meanings, add a `schema_version` and a
reader migration before changing writers.

## Setup and local development

Use a free-threaded Python 3.14 build (`python3.14t`). The development launcher
checks both `Py_GIL_DISABLED` and the runtime GIL state before it starts. Keep
installation separate from server startup:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
./devserver.sh                 # honors PORT, default 8080
```

The reloading development Gunicorn process is for local work only. Production
also uses Gunicorn but without reload mode.
The web page explains credentials and request limits, offers accessible live
status and empty states, and links to `/leaderboard`. `GET /healthz` is a local
health check and never calls OpenRouter.

## Web interaction and bounded runs

The interaction endpoint accepts one prompt. The experiment endpoint permits at
most two batches and three turns across four variants. Every model, string,
integer, body size, and total workload is checked before response streaming
starts. These previews are intentionally much smaller than the research CLI.

Each web run receives a 32-character run ID. The server checkpoints every
completed response before reporting that unit to the browser. To resume after a
timeout or disconnect, submit that run ID with the same model, batch count, and
turn count plus a fresh API key. Saved answers reconstruct conversation context,
so completed paid calls are skipped. Keys and authorization headers never enter
the snapshot. An ordinary nonblocking file lease permits only one request to own
a run ID; a concurrent resume receives HTTP 409 rather than repeating paid work.
Different users and different run IDs use separate files and proceed safely.

`GET /api/experiments/<run-id>/status` returns safe status, completed and total
counts, `heartbeat_at`, `lease_expires_at`, and a derived `liveness` value of
`active`, `stalled`, or `complete`. Each checkpoint renews the 120-second lease.
If an instance disappears without cleanup, the timestamp expires and another
request can claim and resume the stalled run.

`POST /api/experiments/stream` returns `application/x-ndjson`. Events include:

```json
{"type":"status","message":"safe text","completed":2,"total":12,"elapsed_seconds":4.1,"eta_seconds":20.5}
{"type":"success","message":"Experiment complete.","completed":12,"total":12,"elapsed_seconds":25.0,"records":[]}
```

An initial status arrives promptly, statuses separate work units, and exactly
one `success` or sanitized `error` terminates the stream. ETA appears only after
multiple measured units and is a smoothed, approximate duration. There is no
percentage or completion bar. Browser code uses `fetch`, `ReadableStream`,
`TextDecoder`, and a retained partial-line buffer. It renders all remote text
with `textContent`. The loading GIF overlay is an ARIA live region; cancellation,
failure, disconnection, and premature stream closure always restore controls.

Streaming keeps the browser informed but **does not extend Cloud Run's request
deadline**. Full experiments require a future Cloud Run Job, queue, or other
asynchronous worker. The caps target a safety margin under the configured
240-second Gunicorn timeout, but upstream latency still varies.

## Credentials and production security

The CLI reads `OPENROUTER_API_KEY` only as a convenient input and passes it into
the reusable layer explicitly. The browser sends a key only in an HTTPS JSON
request body and clears the field after a terminal outcome. Keys never belong in
URLs, cookies, storage, HTML, logs, exceptions, progress events, JSONL, or
leaderboards. Upstream response details are replaced with stable safe errors.

Before enabling public paid inference, add user authentication, per-user and
global rate limits, spending controls, abuse detection, request audit metadata
that excludes content and secrets, and appropriate Cloud Armor/IAM controls.
Never deploy the service over plain HTTP outside a trusted local environment.

## Leaderboards and persistence

Run `python make_leaderboard.py` to read
`ste_retention_run/records.jsonl` and write `leaderboard.html`. Pure functions
can instead return an HTML string. External values are escaped and numeric
scores must be finite and between 0 and 100. The web route returns a helpful 404
when records do not exist; it never starts an experiment. The committed preview
is synthetic and must remain clearly labeled as such.

Cloud Run's ordinary writable filesystem is ephemeral. The service uses
`EXPERIMENTS_DIR=/experiments` by default, which matches the intended writable
Cloud Storage FUSE mount. Both snapshots and timestamped lease files use only
ordinary access through that mounted directory. The service first attempts a
nonblocking `flock`; if the mount reports locking as unsupported, it falls back
to checking and updating the lease timestamps under a process-local guard. This
fallback is deliberately best-effort across instances because Cloud Storage
FUSE does not promise POSIX file-lock semantics. With the expected small user
count it prevents ordinary duplicate resumes, but it cannot eliminate every
simultaneous cross-instance race. Grant the runtime service account object read,
create, update, and delete access. Local development can set `EXPERIMENTS_DIR`
to a writable temporary directory. A database is the upgrade path if strict
transactional job claiming becomes necessary.

## Quality checks

```sh
pytest
ruff check .
ruff format --check .
python -m compileall -q flask-app.py experiment.py leaderboard.py openrouter.py records.py run_lease.py run_store.py scoring.py statistics_utils.py
```

Tests import the hyphenated entry point safely and mock every inference request;
they never need a live key or paid call. We intentionally do not add Docker-build
tests. If Hadolint is already installed, `hadolint Dockerfile` is an optional
lightweight syntax/style check; do not install a large toolchain just for it.

The image compiles CPython 3.14 with `--disable-gil`, verifies both its build flag
and runtime GIL state, and contains no cooperative-concurrency dependency.
Gunicorn's `gthread` worker uses native threads; `THREADS` defaults to 4. Shared mutable
lease and snapshot registries are protected by explicit `threading.Lock`
instances, while experiment prompt configuration is immutable. The container
runs as a non-root user and expands `${PORT:-8080}` in an `exec`-form shell
command so Gunicorn receives signals correctly. Deploy from the repository root,
for example:

```sh
gcloud run deploy ste-retention --source . --region REGION --allow-unauthenticated \
  --timeout 300 \
  --set-env-vars=PYTHONUNBUFFERED=1,EXPERIMENTS_DIR=/experiments,THREADS=4
```

Do not place a shared OpenRouter key in that environment for this bring-your-own-
key UI. For authenticated server-owned inference, use Secret Manager and a
separate authorization design.

## Troubleshooting

- A 400 response means validation failed before paid work; check model and caps.
- A 502 or streamed error means the provider rejected, timed out, or malformed a
  response. The details are intentionally sanitized; verify the key at OpenRouter.
- A leaderboard 404 means no durable records were mounted or generated.
- If a proxy buffers events, preserve `X-Accel-Buffering: no` and no-cache headers.
- If the stream closes early, the browser reports an error and restores controls;
  retry with less work rather than assuming the experiment completed.

## The question

You can ask a model to write in a controlled style in two ways. You can name the
standard. You can also describe the rules. The two prompts ask for the same
result. They may not work the same way.

A name can work as a key. The model can use the key to find what it learned
about the standard. A description has no key. The model reads the words as
ordinary instructions.

This project measures the difference. It also measures how the difference
changes as the conversation gets longer.

## The design

The experiment crosses two factors. This makes four prompt variants.

| Variant | Names the standard | Gives the rules |
|---|---|---|
| `bare` | No | No |
| `named` | Yes | No |
| `rules` | No | Yes |
| `named_rules` | Yes | Yes |

The cross is necessary. If you compare a short named prompt against a long
prompt with rules, you cannot tell the two effects apart. The difference could
come from the name. The difference could also come from the length. The cross
separates them.

The experiment gives three results:

- The effect of the name, across both levels of rule detail.
- The effect of the rules, across both levels of naming.
- The interaction. A negative value shows that the two overlap.

### Pairs

One session runs all four variants against one model. All four variants get the
same questions in the same order. This makes each result a paired value.
Differences between models do not add noise. Differences between questions do
not add noise. A paired design needs about half the sessions of an unpaired
design.

### Probes

The script scores compliance at three points in the conversation. The default
points are turn 1, turn 6, and turn 12. The script still sends the turns between
the probes. Those turns build the context. The judge model never reads them.
This keeps the cost low.

### Adaptive control

The script runs in batches. It tests the result after each batch. It stops when
the result is clear. It also stops when the money runs out.

If the deepest probe shows no effect, more sessions will not help. The script
then makes the conversation longer instead. It changes the probes to turn 20,
and then to turn 32.

The script tests the result six times. Repeated tests make a false result more
likely. The script therefore uses a stricter limit of p = 0.0158. This is a
Pocock boundary for six looks at a two-sided 0.05 level.

## Before you start

You need free-threaded Python 3.14 (`python3.14t`). Install production packages with:

```
pip install -r requirements.txt
```

You need an OpenRouter key. One key gives access to all the models.

```
export OPENROUTER_API_KEY=sk-or-v1-...
```

## How to get the dictionary

The vocabulary part of the score needs a list of the approved words. The
standard contains that list. You must get your own copy. This project does not
supply one.

### Procedure

1. Open the downloads page at https://www.asd-ste100.org/STE_downloads.html
2. Click **Fill in the request form**. The form is at
   https://forms.gle/7gqWUtj2UK2CBTBv5
3. Complete the form. The STEMG asks for your name, your email address, and
   your organization.
4. Send the form. The STEMG sends the standard to your email address. There is
   no charge.
5. Open the PDF. Find Part 2. Part 2 is the dictionary.
6. Make a text file from the approved entries. Put one word on each line. Use
   lower case. Do not include the words that the standard does not approve.
7. Save the file. Give it the name `approved_words.txt`.
8. Add `approved_words.txt` to your `.gitignore` file. The list comes from the
   standard. You must not publish it.
9. Open `ste_retention.py`. Set `APPROVED_WORDS_FILE` to the path of your file.

### Two cautions

The standard permits technical names and technical verbs that are not in the
dictionary. These are words for your own products and processes. A plain word
list cannot find them. The score will therefore mark some correct words as
wrong. The error applies equally to all four prompt variants, so it does not
change the comparison. It does change the level of the scores.

The standard is a specification, not a word list. The dictionary is one part of
it. The 53 writing rules are the other part. This project tests only some of
those rules. Read the limits section before you use the scores.

## How to run the experiment

1. Review the strict caps shown by `python ste_retention.py --help`.
2. Select the model, batches, turns, and optional records path with CLI flags.
3. Run `python ste_retention.py`.
4. Review the paid request count. Type `y` to start.
5. Run `python make_leaderboard.py` when the experiment stops.
6. Open `leaderboard.html` in a browser.

## The files

### `ste_retention.py`

This file is a thin CLI around the reusable bounded experiment orchestrator.

**Configuration.** CLI flags choose a supported model and bounded workload.
The CLI reads `OPENROUTER_API_KEY`, confirms the paid request count, and passes
the key explicitly to `experiment.py`; reusable functions never read global
credential state.

**Cost estimate.** The script calculates the cost of one batch before it starts.
It asks you to confirm. The estimate includes the input tokens, the output
tokens, and the judge.

**The outer loop.** The loop runs one batch at a time. Each batch runs
`SESSIONS_PER_BATCH` sessions for each model. Each session runs all four
variants.

**One session.** The script sends the constraint in the system prompt. It sends
the constraint one time only. It then asks one question for each turn. It keeps
all replies in the history. It sends the full history with each new question.
Nothing repeats the constraint.

**Scoring.** The script scores a reply at a probe depth only. The first part of
the score is a calculation. The script counts the words in each sentence. It
finds sentences of more than 20 words. It finds the passive voice. It compares
the words against an approved list, if you supply one. The second part of the
score comes from a judge model. The judge reads one passage. The judge does not
know which variant made the passage. The judge does not know the turn number.
The two parts combine into one score from 0 to 100.

**Statistics.** The file includes a t-test. The test does not need SciPy. It
uses an incomplete beta function to find the p-value. This keeps the only
dependency at `requests`.

**Records.** The script writes one line of JSON for each scored reply. Each line
holds the session number, the model, the variant, the depth, the score, the
metrics, the judge value, and the full text. The script writes each line
immediately. A failure does not lose the earlier data.

**Approved words.** ASD owns the copyright and the trademark of ASD-STE100.
This project does not include the dictionary. You must not reprint the standard
in part or in whole in your own documentation.

You can get your own copy at no cost. Request Issue 9 from the STEMG. Refer to
the *License* section below. Then make a word list for your own use. Keep the
list out of the repository.

Set `APPROVED_WORDS_FILE` to the path of your word list. If you do not set it,
the vocabulary part of the score drops out. The other parts continue to work.

### `make_leaderboard.py`

This file makes the web page. It does not call any model. It reads the records
and writes one HTML file.

**Input.** The script reads `ste_retention_run/records.jsonl`. It groups the
records by session, model, and depth. It keeps a group only when all four
variants are present. An incomplete group would break the pairing.

**Calculation.** The script calculates the three contrasts for each complete
group. It then runs a one-sample t-test on each contrast. It uses the same test
function as the experiment. This makes sure that the two files agree.

**The chart.** The chart is at the top of the page. Each row shows one factor at
one depth. A dot shows the size of the effect. A bar shows the 95 percent
confidence interval. A vertical line marks zero. A bar that crosses the line
shows no reliable effect. The script draws the chart as SVG. It uses no chart
library.

**The tables.** Three tables follow the chart. The first table gives the
numbers behind the chart. The second table gives the mean score for each of the
four variants. The third table gives the naming effect for each model. The third
table is a secondary result. Each model has fewer sessions than the pooled
total.

**Output.** The script writes `leaderboard.html`. The file is complete in
itself. It needs no server. You can put the file on any web host.

### `leaderboard_preview.html`

This file is an example. It shows the layout.

**The data is false.** A script made the data. No model made the data. Do not
use the numbers. Do not publish the file.

The file lets you look at the design before you spend money. You can change the
colors and the layout in the `CSS` string in `make_leaderboard.py`. When you run
the real experiment, the same code writes a new file with true data.

## Cost

OpenRouter pricing changes and depends on model and tokens. The application does
not claim a price estimate; inspect current provider pricing and spending limits
before confirming any run. The web preview is capped at 24 calls, and the CLI
uses the same bounded orchestrator. Larger research runs require an explicitly
designed asynchronous worker and budget controls.

## Limits of this work

The score is not a full test of ASD-STE100. Without the approved word list, the
score measures sentence length and the active voice only. The judge model adds
an opinion. The judge is not a certified checker.

The result applies to the models in the list on the day of the run. Models
change. The result does not transfer to other standards without a new
experiment.

The per-model results use no correction for multiple comparisons. Read them as
an indication. Do not read them as a ranking.

## The standard

ASD owns ASD-STE100. It is a copyright and a European Union trademark
(No. 017966390) of ASD, Brussels, Belgium. The current version is Issue 9,
January 2025.

The standard costs nothing. Request an official copy from the STEMG:

- Request form: https://forms.gle/7gqWUtj2UK2CBTBv5
- Downloads page: https://www.asd-ste100.org/STE_downloads.html

Older pages tell you to buy the standard from a distributor. That information is
no longer correct.

### What you can do

You can use the standard to write documentation. You do not need permission.

### What you must not do

- Do not reprint the standard, in part or in whole, in your own documentation.
- Do not change the standard.
- Do not use the ASD logo, copyright, or trademark in your material.
- Do not say that ASD approves or certifies this project. ASD does not approve
  or certify any software. ASD applies the same policy to AI tools.

If you make a product from the standard, such as a checker, you must first ask
ASD for permission. Write to stemg@asd-ste100.org.

This project measures how models behave. It is not a checker and it is not a
product. It gives no STE certification.

## Related work

The STEMG has an Artificial Intelligence Task Team (AITT). The team released a
white paper in June 2026.

https://www.asd-ste100.org/assets/files/WhitePaper-ASD-STE100_and_AI.pdf

The white paper makes two points that apply to this project. First, AI text can
look correct but can still break the rules of the standard. Second, the STEMG
lists an evaluation framework for AI performance in STE writing as future work
that it wants.

If you publish results from this project, tell the STEMG. Their address is
stemg@asd-ste100.org.

## Data

Publish the raw replies with the results. A leaderboard has value only when
another person can check it.

Do not publish your approved word list. It comes from the standard.
