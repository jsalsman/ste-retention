"""Cloud Run Flask entry point for the STE retention demonstration."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from ste.experiment import ALLOWED_MODELS, run_experiment, validate_run
from ste.leaderboard import render_file, render_leaderboard
from ste.models.openrouter import UpstreamError, chat
from ste.records import RecordError
from ste.research import ResearchConfig, load_state, new_state, run_research, save_state
from ste.runs.lease import RunActiveError, acquire_lease
from ste.runs.store import RUN_ID, RunStoreError, completed_records, create_run, load_run, save_run

# All repository assets resolve from this file rather than the process directory.
ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
RECORDS = ROOT / "ste_retention_run" / "records.jsonl"
EXPERIMENTS = Path(os.environ.get("EXPERIMENTS_DIR", "/experiments"))
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config.update(MAX_CONTENT_LENGTH=16 * 1024, JSON_SORT_KEYS=False)


def _payload() -> dict:
    """Return an object JSON body or raise a predictable validation error."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        # A stable message helps both browser and API clients recover.
        raise ValueError("Send a JSON object request body.")
    return data


def _key(data: dict) -> str:
    """Extract an ephemeral API key without retaining or echoing its value."""
    value = data.pop("api_key", None)
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        # Do not include the submitted value in any exception.
        raise ValueError("A valid OpenRouter API key is required.")
    return value.strip()


@app.get("/")
def index():
    """Serve the standalone, non-Jinja application document."""
    if not INDEX.is_file():
        # A safe JSON response makes incomplete deployments diagnosable.
        return jsonify(error="The application page is unavailable."), 503
    return send_file(INDEX)


@app.get("/api/healthz")
def health():
    """Report process health without contacting paid or external services."""
    return jsonify(status="ok")


@app.get("/leaderboard")
@app.get("/leaderboard.html")
def leaderboard_page():
    """Render persisted results or return a helpful safe empty-state response."""
    mounted_records = completed_records(EXPERIMENTS)
    if not mounted_records and not RECORDS.is_file():
        return Response(
            "<h1>Leaderboard unavailable</h1><p>No experiment results have been persisted yet.</p>",
            status=404,
            content_type="text/html; charset=utf-8",
        )
    try:
        # Prefer durable mounted runs, while retaining the legacy JSONL migration path.
        rendered = render_leaderboard(mounted_records) if mounted_records else render_file(RECORDS)
        return Response(rendered, content_type="text/html; charset=utf-8")
    except (OSError, RecordError, ValueError):
        return jsonify(error="Leaderboard records are malformed or unavailable."), 500


@app.post("/api/interact")
@app.post("/interact")
def interact():
    """Perform one bounded model interaction while keeping its key ephemeral."""
    try:
        data = _payload()
        api_key = _key(data)
        model, prompt = data.get("model"), data.get("prompt")
        if model not in ALLOWED_MODELS:
            raise ValueError("Select a supported model.")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise ValueError("Prompt text must contain between 1 and 2,000 characters.")
        # The credential exists only in this call frame and never enters the response.
        answer = chat(api_key, model, [{"role": "user", "content": prompt.strip()}])
        return jsonify(answer=answer)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except UpstreamError:
        return jsonify(error="The model provider could not complete the request."), 502


@app.post("/api/experiments/stream")
@app.post("/run-experiment")
def experiment_stream():
    """Run or resume a preview or complete research study as safe NDJSON."""
    try:
        data = _payload()
        api_key = _key(data)
        mode = data.get("run_mode", "preview")
        if mode == "research":
            return _research_stream(data, api_key)
        if mode != "preview":
            raise ValueError("Select a supported experiment type.")
        model, batches, turns = validate_run(
            data.get("model"), data.get("batches"), data.get("turns")
        )
        resume_id = data.get("resume_run_id")
        if resume_id in (None, ""):
            state = create_run(EXPERIMENTS, model, batches, turns)
        elif isinstance(resume_id, str):
            state = load_run(EXPERIMENTS, resume_id)
            # Never resume saved work under changed parameters or a different model.
            if (state["model"], state["batches"], state["turns"]) != (model, batches, turns):
                raise ValueError("Saved run settings do not match this request.")
        else:
            raise ValueError("The experiment run identifier is invalid.")
        # The open lease handle remains held for the entire streamed request.
        lease = acquire_lease(state["run_id"], EXPERIMENTS)
        state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        state["lease_expires_at"] = lease.expires_at.isoformat()
        state["status"] = "running"
        save_run(EXPERIMENTS, state)
    except RunActiveError as exc:
        return jsonify(error=str(exc)), 409
    except (OSError, RunStoreError, ValueError) as exc:
        return jsonify(error=str(exc)), 400

    def checkpoint(records: list[dict]) -> None:
        """Persist a credential-free snapshot after one completed inference unit."""
        expires = lease.heartbeat()
        state["records"] = list(records)
        state["status"] = "running"
        state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        state["lease_expires_at"] = expires.isoformat()
        # The API key is held only by the surrounding request and never enters state.
        save_run(EXPERIMENTS, state)

    @stream_with_context
    def generate():
        """Yield exactly one terminal event even when inference fails after headers."""
        terminal = False
        completed = 0
        elapsed = 0.0
        try:
            for event in run_experiment(
                api_key,
                model,
                batches,
                turns,
                existing_records=state["records"],
                persist=checkpoint,
                run_id=state["run_id"],
                seed=state["seed"],
            ):
                # The web layer guarantees a resume handle even for injected orchestrators.
                event.setdefault("run_id", state["run_id"])
                if event.get("type") == "success":
                    # Commit terminal status before telling the client the run is complete.
                    state["records"] = list(event.get("records", state["records"]))
                    state["status"] = "complete"
                    state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
                    state["lease_expires_at"] = None
                    save_run(EXPERIMENTS, state)
                terminal = event.get("type") in {"success", "error"}
                completed = int(event.get("completed", completed))
                elapsed = float(event.get("elapsed_seconds", elapsed))
                # One compact JSON object per line remains independently parseable.
                yield json.dumps(event, ensure_ascii=False) + "\n"
            if not terminal:
                # A normally exhausted generator without a terminal event is premature.
                raise RuntimeError("Experiment generator ended prematurely.")
        except Exception:  # noqa: BLE001
            if not terminal:
                state["status"] = "interrupted"
                state["lease_expires_at"] = None
                try:
                    # Best-effort failure metadata leaves completed checkpoints resumable.
                    save_run(EXPERIMENTS, state)
                except (OSError, RunStoreError):
                    pass
                # Sanitize all upstream and unexpected exception details.
                yield (
                    json.dumps(
                        {
                            "type": "error",
                            "message": "The experiment stopped safely.",
                            "completed": completed,
                            "total": batches * turns * 4,
                            "elapsed_seconds": elapsed,
                            "run_id": state["run_id"],
                        }
                    )
                    + "\n"
                )
        finally:
            if not terminal and state.get("status") != "interrupted":
                # Client cancellation closes the generator without an exception we can stream.
                state["status"] = "interrupted"
                state["lease_expires_at"] = None
                try:
                    save_run(EXPERIMENTS, state)
                except (OSError, RunStoreError):
                    # The connection is already closing, so no response channel remains.
                    pass
            lease.release()

    response = Response(generate(), content_type="application/x-ndjson")
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        }
    )
    return response


def _research_stream(data: dict, api_key: str) -> Response:
    """Start or resume the full research protocol for the selected model.

    The research path uses the same orchestrator as ``python -m ste.research``.
    It checkpoints each paid unit, and the API key stays only in this request.
    """
    model = data.get("model")
    sessions = data.get("sessions", 6)
    config = ResearchConfig((model,), sessions)
    config.validate()
    resume_id = data.get("resume_run_id")
    if resume_id in (None, ""):
        state = new_state(config)
    elif isinstance(resume_id, str) and RUN_ID.fullmatch(resume_id):
        # The explicit configuration prevents a run from resuming with new semantics.
        state = load_state(EXPERIMENTS / f"{resume_id}.json", config, resume_id)
    else:
        raise ValueError("The experiment run identifier is invalid.")
    path = EXPERIMENTS / f"{state['run_id']}.json"
    lease = acquire_lease(state["run_id"], EXPERIMENTS)
    state["status"] = "running"
    state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    state["lease_expires_at"] = lease.expires_at.isoformat()
    save_state(path, state)

    def checkpoint(value: dict) -> None:
        """Save one credential-free research checkpoint and renew its lease."""
        expires = lease.heartbeat()
        value["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        value["lease_expires_at"] = expires.isoformat()
        # Research state contains model output, but it never contains the API key.
        save_state(path, value)

    @stream_with_context
    def generate():
        """Translate research-worker events to independently valid NDJSON lines."""
        started = datetime.now(timezone.utc)
        completed = len(state["units"])
        completed_at_start = completed
        total = config.workload()["total_calls"]
        terminal = False
        try:
            yield (
                json.dumps(
                    {
                        "type": "status",
                        "message": "Full research study started.",
                        "completed": completed,
                        "total": total,
                        "elapsed_seconds": 0.0,
                        "run_id": state["run_id"],
                    }
                )
                + "\n"
            )
            for worker_event in run_research(api_key, config, state, checkpoint):
                elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
                if worker_event["type"] == "success":
                    terminal = True
                    state["lease_expires_at"] = None
                    save_state(path, state)
                    event = {
                        "type": "success",
                        "message": "Full research study complete.",
                        "completed": total,
                        "total": total,
                        "elapsed_seconds": round(elapsed, 2),
                        "run_id": state["run_id"],
                    }
                else:
                    completed += 1
                    measured = completed - completed_at_start
                    event = {
                        "type": "status",
                        "message": f"Saved research call {completed} of {total}.",
                        "completed": completed,
                        "total": total,
                        "elapsed_seconds": round(elapsed, 2),
                        "run_id": state["run_id"],
                    }
                    # An ETA needs at least two measured calls to be useful.
                    if measured >= 2 and elapsed > 0:
                        event["eta_seconds"] = round(elapsed / measured * (total - completed), 1)
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception:  # noqa: BLE001
            if not terminal:
                state["status"] = "interrupted"
                state["lease_expires_at"] = None
                try:
                    save_state(path, state)
                except OSError:
                    pass
                yield (
                    json.dumps(
                        {
                            "type": "error",
                            "message": "The research study stopped safely.",
                            "completed": completed,
                            "total": total,
                            "elapsed_seconds": round(
                                max(0.0, (datetime.now(timezone.utc) - started).total_seconds()), 2
                            ),
                            "run_id": state["run_id"],
                        }
                    )
                    + "\n"
                )
        finally:
            if not terminal and state.get("status") != "interrupted":
                state["status"] = "interrupted"
                state["lease_expires_at"] = None
                try:
                    save_state(path, state)
                except OSError:
                    pass
            lease.release()

    response = Response(generate(), content_type="application/x-ndjson")
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        }
    )
    return response


@app.get("/api/experiments/<run_id>/status")
def experiment_status(run_id: str):
    """Report persisted progress and timestamp-derived active or stalled liveness."""
    try:
        if not RUN_ID.fullmatch(run_id):
            raise RunStoreError("The experiment run identifier is invalid.")
        path = EXPERIMENTS / f"{run_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RunStoreError("The saved experiment run is malformed.")
        if raw.get("run_mode") == "research":
            config_data = raw.get("config", {})
            config = ResearchConfig(
                tuple(config_data.get("models", ())),
                config_data.get("sessions"),
                tuple(config_data.get("depths", ())),
                config_data.get("seed", 0),
                config_data.get("budget_usd", 40.0),
                config_data.get("provider_timeout", 120.0),
                config_data.get("judge_model"),
                config_data.get("judge_timeout", 120.0),
                config_data.get("max_tokens", 600),
            )
            state = load_state(path, config, run_id)
            completed = len(state["units"])
            total = config.workload()["total_calls"]
        else:
            state = load_run(EXPERIMENTS, run_id)
            completed = len(state["records"])
            total = state["batches"] * state["turns"] * 4
        expiry_text = state.get("lease_expires_at")
        expiry = datetime.fromisoformat(expiry_text) if expiry_text else None
        now = datetime.now(timezone.utc)
        # A complete run is terminal; an unexpired lease is active; all others are stalled.
        liveness = (
            "complete"
            if state["status"] == "complete"
            else ("active" if expiry and expiry > now else "stalled")
        )
        return jsonify(
            run_id=run_id,
            status=state["status"],
            liveness=liveness,
            completed=completed,
            total=total,
            heartbeat_at=state.get("heartbeat_at"),
            lease_expires_at=expiry_text,
            updated_at=state.get("updated_at"),
        )
    except (OSError, json.JSONDecodeError, RunStoreError, TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 404


@app.delete("/api/experiments/<run_id>")
def delete_experiment(run_id: str):
    """Delete an explicitly identified experiment snapshot."""
    try:
        if not RUN_ID.fullmatch(run_id):
            raise RunStoreError("The experiment run identifier is invalid.")
        # The narrow identifier validation makes the derived path traversal-safe.
        (EXPERIMENTS / f"{run_id}.json").unlink()
        return Response(status=204)
    except (OSError, RunStoreError) as exc:
        return jsonify(error=str(exc)), 404


@app.after_request
def security_headers(response):
    """Apply browser and transport defenses to every application response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    )
    if request.is_secure:
        # HSTS is meaningful only after TLS termination marks the request secure.
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@app.errorhandler(405)
def method_not_allowed(_error):
    """Return a predictable response for unsupported methods."""
    return jsonify(error="Method not allowed."), 405


@app.errorhandler(413)
def request_too_large(_error):
    """Reject oversized bodies before parsing or paid upstream work."""
    return jsonify(error="Request body is too large."), 413


@app.errorhandler(500)
def internal_error(_error):
    """Hide internal details, headers, and credentials from error responses."""
    return jsonify(error="An internal error occurred."), 500
