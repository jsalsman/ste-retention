# STE Retention

This repository tests whether naming ASD-STE100 helps a model retain simplified-technical-English constraints. It provides two deliberately separate execution paths:

* The Flask application provides a bounded **preview**: one batch, at most three turns, and four arms (at most 12 generation calls). It is not the study.
* `python -m ste.research` is the resumable **research worker/Cloud Run Job**. It uses the complete 32-prompt pool, all four factorial arms, and probes at turns 1, 6, and 12 by default. It is never invoked by an HTTP request.

`index.html` remains a standalone document. Flask transport stays in `flask-app.py`; reusable protocol, orchestration, provider, scoring, statistics, record, leaderboard, and run components live in the installable `ste` package. The files under `originals/` are immutable historical references and are deliberately excluded from packaging and Ruff.

## Protocol and research fidelity

`ste/protocol.py` is the authority for protocol version `ste-retention-2.0`, scoring version `mechanical-2.0`, schema version 2, model policy, prompt pool, variants, complete rule text, record fields, preview caps, and default research probes. `rules` and `named_rules` use exactly the same complete rule suffix. Within each session, every arm receives the same deterministic prompt sequence. The sequence depends on the stored run seed and session identity—not merely a repeated batch number.

The 2×2 arms are `bare`, `rules`, `named`, and `named_rules`. Paired contrasts are:

* rule detail = `((rules - bare) + (named_rules - named)) / 2`;
* naming = `((named - bare) + (named_rules - rules)) / 2`;
* interaction = `named_rules - named - rules + bare`.

These package organization, full prompt pool, probe depths, complete instructions, optional licensed vocabulary, optional judge, and factorial contrasts are the useful research-fidelity ideas retained from `arch-1`. The stronger `arch-2` validation, checkpoint-before-report ordering, atomic snapshots, explicit resume identity, leases, sanitized terminal events, and HTTP limits remain intact.

## Setup and local quality gates

Use CPython 3.14.7 or newer. This workload waits on networks and files; no measured result justified compiling a custom free-threaded interpreter. Production uses pinned `python:3.14.7-slim-trixie`, Gunicorn `gthread`, one worker by default, and a non-root user. Multiple workers are safe only when production run ownership is transactional as discussed below.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check .
ruff format --check .
python -m compileall -q flask-app.py ste tests
./devserver.sh
```

Tests mock every provider and judge call. They need no credential, network, paid inference, or
Docker daemon. The application is run locally only through `devserver.sh`; the Dockerfile belongs
to Cloud Build rather than the local development workflow. The other commands are local
pre-commit checks; this repository does not install a
GitHub Actions deployment workflow. Cloud Build is the deployment build system for this branch.

## Bounded web preview

`POST /api/experiments/stream` accepts JSON objects only and validates model, integers, strings, request size, and a maximum of 12 units before streaming. Provider calls have a nine-second timeout; the orchestrator has a 240-second interactive deadline; Gunicorn uses 270 seconds plus a 30-second graceful timeout; a Cloud Run request timeout should be at least 300 seconds. **NDJSON streaming does not extend the Cloud Run request deadline.**

The server sends an initial status promptly, checkpoints every paid response before announcing it, and emits one independently valid JSON object per line. Every writable stream ends in exactly one sanitized `success` or `error`. `Cache-Control: no-store`, `X-Accel-Buffering: no`, `nosniff`, CSP, referrer, and permissions headers are applied. The browser handles arbitrary chunks and a final unterminated line, inserts remote content only with `textContent`, uses the loading GIF in an accessible overlay, and restores controls after every outcome. It intentionally has no percentages or progress bar.

A resume requires an explicit run ID and identical settings. The stored random seed recreates prompts. Saved responses rebuild conversation context, completed units are skipped, and duplicate, excess, malformed, conflicting, and out-of-range records fail closed. `GET /api/experiments/<id>/status` returns safe liveness metadata. `DELETE /api/experiments/<id>` deletes an owner's operational snapshot.

## Full research worker

Example dry configuration and confirmed invocation:

```sh
export OPENROUTER_API_KEY='...'
python -m ste.research \
  --models google/gemini-2.0-flash-001 openai/gpt-4o \
  --sessions 6 --depths 1 6 12 --seed 20260916 \
  --budget-usd 40 --provider-timeout 120 \
  --approved-words /secure/approved_words.txt \
  --judge-model anthropic/claude-sonnet-4.5 --judge-timeout 120 \
  --state /durable/research/RUN.json --yes
```

The worker displays generation calls, separate judge calls, and the operator's budget cap before paid work; `--yes` is mandatory. Pricing is intentionally not hard-coded because provider prices change: configure provider-side spending limits at or below the confirmed cap. Sessions, models, depths, seed, timeouts, judge, and budget are persisted and validated on resume. Use `--run-id ID` with the same state path and configuration; the worker never chooses a stale run implicitly. Every generation and judge unit has an idempotency identity and is atomically persisted before it is reported. Partial arms resume at the next missing turn with reconstructed history.

An approved-word file is optional and supplied only at runtime; licensed data is ignored by Git and is never committed. Judge output must be a JSON object with one finite `score` from 0 through 100. Credentials are passed explicitly and are absent from snapshots, records, errors, output, and logs.

## Scoring limitations

Mechanical scoring reports sentence count/length, compliance with the 25-word descriptive ceiling, a regex-based active/passive estimate, and vocabulary compliance when a list exists. It retains the original over-20-word diagnostic. Optional components are `null`, not success or failure, when unavailable. The composite is the equal-weight arithmetic mean of available component percentages (sentence length, active voice, optional vocabulary, optional judge), bounded from 0 through 100. Regex sentence boundaries and passive detection have false positives and negatives; technical names may be valid despite absence from a word list. This is transparent approximation, **not ASD-STE100 certification**.

## Schema and migration

Every schema-2 run and analytical record carries `schema_version`, `protocol_version`, `scoring_version`, `run_mode`, stable run/session identities, model, arm, depth, prompt/sequence identities, score, component metrics, and useful timestamps. Operational snapshots additionally contain prompts/responses needed for resume. Current readers reject incompatible version triples rather than pooling them. Leaderboards separate version strata and, by default, exclude previews, synthetic data, incomplete cells, and retries that are not complete.

The only automatic migration is the documented original JSONL shape containing `session`, `model`, `variant`, `depth`, and `score`. It is labeled `schema_version: 1`, `protocol_version: legacy-original`, and `scoring_version: legacy-original`; fields are not reinterpreted. Other historical shapes require an explicit offline adapter and produce a line-numbered error.

## Authentication, authorization, and production coordination

Local mode defaults to user `local`. **Do not expose it publicly.** Public deployment must set `AUTH_REQUIRED=true` and configure `AUTH_TOKENS_JSON` through Secret Manager (never a checked-in environment file), or replace `ste.auth` with verified Cloud Run IAM identity. All start, resume, view, and delete operations are owner-scoped; run IDs are identifiers, never authorization secrets. The included process-local burst limit is defense in depth, not a global limit.

Before public paid inference, enforce at an authenticated gateway/transactional database:

* per-user and global request and spending limits, maximum concurrent runs, abuse detection, and safe content-free audit metadata;
* Cloud Run IAM with `--no-allow-unauthenticated` and, when needed, Cloud Armor; TLS is mandatory;
* CSRF tokens if authentication ever moves to cookies (the supplied bearer design does not use cookies);
* a transactional ownership row claimed with a conditional update, for example `UPDATE runs SET owner_worker=:worker, lease_until=:expiry WHERE run_id=:id AND (owner_worker IS NULL OR lease_until < CURRENT_TIMESTAMP)`, followed by checking exactly one affected row;
* a unique database constraint on `(run_id, unit_id)` so repeated checkpoints are idempotent and duplicate paid calls cannot become duplicate observations.

Local `flock`, heartbeat, timestamp fallback, atomic replacement, and process locks are useful development behavior. Timestamp fallback plus a process-local lock is **not exactly-once cross-instance coordination**. Do not run multiple production instances/workers for paid work until the transactional owner adapter above is installed. A managed task queue with run/unit idempotency keys is an equivalent production option.

## Retention, deletion, and recovery

`EXPERIMENT_RETENTION_DAYS` should be enforced by a scheduled authenticated cleanup job (recommended default 30 days): delete expired completed, interrupted, and abandoned operational snapshots plus leases. The API deletion route removes only the caller's operational preview state. Research snapshots and de-identified analytical records are separate assets; deleting operational state must not silently delete authorized research data. Establish the study's analytical retention/consent period explicitly before collection (the repository sets none).

Snapshots contain prompts and model responses. Limit access to the owner and authorized operators, encrypt storage and backups, and disclose this storage before collection. Back up authorized analytical records with tested restore procedures; resumable operational state can use short-lived encrypted backups. Logs must contain only user/run/unit identifiers, status, timing, and sanitized provider categories—never credentials, authorization headers, prompts, responses, provider bodies, or licensed words.

## Deployment

The Dockerfile is the build contract for a Cloud Build-backed Cloud Run deployment. Configure the
Cloud Build trigger and the target Cloud Run service before expecting a remote container build to
run. The service configuration must include, at minimum:

* the region, runtime service account, ingress and IAM policy;
* memory, CPU, concurrency, minimum/maximum instances, and the 300-second request timeout;
* the writable GCS bucket volume and its `/experiments` FUSE mount;
* the default `/experiments` storage path and Cloud Run IAM authentication policy;
* health/startup probes for `/api/healthz`; and
* transactional ownership/global limit infrastructure before enabling paid multi-instance work.

Do not use `--allow-unauthenticated` for a service that can initiate paid calls. Use a separate
Cloud Run Job for `python -m ste.research`. No service-level OpenRouter key, related environment
variable, or Secret Manager entry is required: the user supplies that credential in each request,
and the service does not persist it. The container preserves signal forwarding through `exec`;
`/api/healthz` and `/` are its build-time smoke-test targets. Those image checks do not prove that
the Cloud Build trigger, service memory, IAM, or GCS FUSE mount are configured correctly.

## Deliberate scope choices

* No licensed approved-word data, credentials, run data, or responses are committed.
* The web limit remains one batch rather than two because no benchmark justifies raising it.
* Adaptive sequential stopping from the historical script is documented research context but is not silently automated: operators configure session/depth and budget caps, while provider-side budget enforcement is authoritative.
* A custom free-threaded CPython build was removed because the service is I/O-bound and no measured benefit justified its build time, image size, or supply-chain surface.
* Cross-instance exactly-once ownership, distributed global spending/rate enforcement, backup service, and scheduled retention execution are deployment infrastructure contracts, not falsely simulated with filesystem timestamps.

ASD-STE100 is ASD's copyrighted standard and trademark. Obtain an official copy under its terms; this project does not redistribute its dictionary.
