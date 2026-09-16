import json
import math
import os
import random
from collections import deque
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from ste.leaderboard import generate_leaderboard_html
from ste.orchestration import (
    TASK_PROMPTS,
    VARIANTS,
    get_incomplete_session,
    run_session_generator,
)
from ste.orchestration.io import append_record, get_records_file
from ste.scoring import load_approved_words

app = Flask(__name__)

# Constants
RECORDS_FILE = get_records_file()
APPROVED_WORDS_FILE = None
JUDGE_MODEL = "anthropic/claude-sonnet-4.5"
MODELS = [
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-4o",
    "google/gemini-2.0-flash-001",
]
DEPTH_SCHEDULE = [1, 6, 12]

# Ensure run directory exists
os.makedirs(os.path.dirname(RECORDS_FILE) if os.path.dirname(RECORDS_FILE) else ".", exist_ok=True)


@app.route("/")
def index():
    return send_file("index.html")


@app.route("/static/<path:path>")
def static_files(path):
    return send_file(f"static/{path}")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/leaderboard")
def api_leaderboard():
    # Return partial=True so it doesn't wrap in <html> tags for the frontend AJAX injection
    html = generate_leaderboard_html(RECORDS_FILE, partial=True)
    return Response(html, mimetype="text/html")


@app.route("/api/experiment/stream", methods=["POST"])
def experiment_stream():
    """
    Runs a bounded experiment and streams NDJSON results.
    Workload caps apply here to fit within Cloud Run timeouts (120s max).
    It dynamically picks up where it left off for the requested model.
    """
    data = request.json
    api_key = data.get("api_key")
    if not api_key:
        return jsonify({"error": "Missing OpenRouter API Key"}), 401

    model = data.get("model", MODELS[0])
    if model not in MODELS:
        return jsonify({"error": "Unsupported model"}), 400

    # Check for incomplete sessions to resume
    session_id, variants_to_run = get_incomplete_session(
        RECORDS_FILE, model, max_depth=max(DEPTH_SCHEDULE)
    )
    if not variants_to_run:
        variants_to_run = list(VARIANTS.keys())

    # Deterministic seeding so resumption gets the exact same prompts
    rng = random.Random(session_id)
    prompts = rng.sample(TASK_PROMPTS, max(DEPTH_SCHEDULE))
    approved = load_approved_words(APPROVED_WORDS_FILE)

    # Pre-compute total expected turns to calculate ETA properly
    total_turns = len(variants_to_run) * max(DEPTH_SCHEDULE)

    def generate():
        completed_turns = 0
        turn_times = deque(maxlen=4)

        def write_heartbeat(ev_type, variant):
            heartbeat = {
                "type": "heartbeat_" + ev_type,
                "session": session_id,
                "model": model,
                "variant": variant,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            append_record(heartbeat, RECORDS_FILE)

        try:
            yield (
                json.dumps(
                    {
                        "type": "start",
                        "message": f"Starting/Resuming session {session_id}...",
                        "model": model,
                        "total_turns": total_turns,
                    }
                )
                + "\n"
            )

            for variant in variants_to_run:
                for event in run_session_generator(
                    model,
                    variant,
                    prompts,
                    DEPTH_SCHEDULE,
                    approved,
                    session_id,
                    api_key,
                    judge_model=JUDGE_MODEL,
                ):
                    if event["type"] == "session_start":
                        write_heartbeat("session_start", variant)

                    elif event["type"] == "turn_complete":
                        write_heartbeat("turn_complete", variant)
                        completed_turns += 1
                        turn_times.append(event["turn_time"])

                        # ETA calculation
                        eta_text = None
                        if len(turn_times) > 0:
                            avg_time = sum(turn_times) / len(turn_times)
                            remaining_turns = total_turns - completed_turns
                            eta_seconds = remaining_turns * avg_time
                            eta_text = f"~{math.ceil(eta_seconds)}s remaining"

                        yield (
                            json.dumps(
                                {
                                    "type": "status",
                                    "message": f"Completed turn {completed_turns}/{total_turns}",
                                    "completed": completed_turns,
                                    "total": total_turns,
                                    "eta": eta_text,
                                }
                            )
                            + "\n"
                        )

                    elif event["type"] == "record":
                        append_record(event["record"], RECORDS_FILE)

            yield (
                json.dumps(
                    {
                        "type": "success",
                        "message": "Experiment completed successfully. Refreshing leaderboard...",
                    }
                )
                + "\n"
            )

        except Exception as e:
            error_msg = str(e)
            if api_key in error_msg:
                error_msg = error_msg.replace(api_key, "[REDACTED]")

            yield json.dumps({"type": "error", "message": f"Experiment failed: {error_msg}"}) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
