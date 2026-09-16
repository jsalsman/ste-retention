# STE Retention

This project answers one question: Does a model follow a writing standard better when you give the standard a name? It measures the difference across varying conversation depths.

## Flask Application & Web UI

The project has been expanded into a fully hosted web service via Flask, allowing you to run bounded versions of the experiment on demand and view the leaderboard dynamically.

### Key Features
- **Standalone Frontend**: A clean `index.html` at the repository root provides the UX for running experiments and viewing results.
- **Bounded Experiment Streaming**: The `/api/experiment/stream` endpoint streams progress (using NDJSON and `stream_with_context()`) to an accessible loading overlay. It provides a textual ETA and ensures execution safely finishes within Cloud Run's 120-second timeout.
- **Secure Credentials**: API keys are securely transmitted to memory for the request duration and are aggressively redacted from errors and logs to prevent leakage.
- **Dynamic Leaderboard Serving**: The backend processes results in-memory and dynamically serves a rendered HTML leaderboard.

### Local Development

1. Set up your virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt -r requirements-dev.txt
   ```
2. Start the development server (auto-reloading enabled):
   ```bash
   ./devserver.sh
   ```
3. Run tests using `pytest` and lint with `ruff`:
   ```bash
   pytest tests/test_app.py
   ruff check .
   ruff format --check .
   ```

### Deployment (Google Cloud Run)
The provided `Dockerfile` is optimized for Cloud Run. It uses a lightweight Python slim image, separates production dependencies, avoids root execution, and launches using Gunicorn with Gevent for NDJSON streaming compatibility. Cloud Run ephemeral file storage is used to aggregate data temporarily. For durable persistence, connect an external storage bucket.

---

## The Original Experiment

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

### Procedure

1. Obtain your official ASD-STE100 standard and build `approved_words.txt` based on the official dictionary. Do not commit this list to source control.
2. The CLI implementation `ste_retention.py` allows testing multiple models over sequential batches to test for statistical significance bounds.
3. The results aggregate into `ste_retention_run/records.jsonl`.

---

*This repository has been fully modernized and refactored from a procedural CLI script into a scalable, multi-tenant Flask web application using native Python 3.14t free-threading, safe bounded execution, and robust liveness tracking.*
