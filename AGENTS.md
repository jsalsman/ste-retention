# Guidelines for AI Agents working on this project

- Code Style/Documentation Requirements: All functions must have thorough docstrings (JSDoc for JS). Inline comments must be present on at least 2 out of every 7 lines of code, with a minimum of 2 inline comments per function/block. Global constants and structures require thorough introductory comments describing their purpose and use.

- The frontend is served from a standalone `index.html` file at the root. Do NOT use a `templates/` folder or Jinja expressions for the web UI.
- Keep CSS in `static/styles.css` and behavior in `static/app.js`.
- Bounded experiments stream progress to the web using NDJSON and `stream_with_context()`.
- Ensure textual ETA is calculated conservatively based on measured completed work and displayed without completion bars.
- The loading overlay (`static/loading.gif`) must use ARIA live regions and provide safe status/error updates.
- External model API credentials (like OpenRouter) must be redacted from all errors, logs, responses, and records, and must never be persisted.
- Experiment runs from the UI are strictly bounded (e.g. one full session) to complete within the 120-second timeout of Google Cloud Run. Longer asynchronous runs require alternative worker architectures.
- External LLM inference calls must be fully mocked in tests to prevent unintended API usage or billing during CI.
- Tests should remain focused in `tests/test_app.py` on routes and app behavior. Avoid writing Docker-build tests that pull significant toolchains.
- Production and dev dependencies are strictly separate (`requirements.txt` vs `requirements-dev.txt`) to keep the production container slim.
- `ruff` is configured for formatting and checking; use it prior to committing.
