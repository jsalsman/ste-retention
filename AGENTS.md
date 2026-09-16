# Guidelines for AI Agents working on this project

- Code Style/Documentation Requirements: All functions must have thorough docstrings (JSDoc for JS). Inline comments must be present on at least 2 out of every 7 lines of code, with a minimum of 2 inline comments per function/block. Global constants and structures require thorough introductory comments describing their purpose and use.

- Keep the standalone application document at root-level `index.html`; do not add a `templates/` directory or Jinja markup.
- Keep presentation in `static/styles.css` and browser behavior in focused `static/app.js` code.
- Wrap every Flask streaming generator with `stream_with_context()`.
- Frame streamed progress as one independently valid JSON object per newline.
- Derive textual ETA from completed measured work, label it approximate, and omit it before it is meaningful.
- Show `static/loading.gif` in the accessible overlay during active experiment requests.
- Restore controls and hide the overlay after success, failure, cancellation, disconnection, or premature closure.
- Do not introduce percentage displays, completion bars, or progress elements.
- Pass credentials explicitly, redact them from every output, and never persist or log them.
- Bound synchronous web work below the deployment request timeout with a safety margin.
- Direct full experiments to an asynchronous worker such as a Cloud Run Job.
- Escape external leaderboard values and validate every numeric chart or SVG input.
- Preserve keyboard operation, semantic labels, live status, contrast, and reduced-motion support.
- Mock all paid and external calls in tests, which belong under `tests/`.
- Run both `ruff check` and `ruff format --check` before committing Python changes.
- Keep runtime requirements separate from development and test requirements.
- Do not add tests that build or run the Docker container.
- Keep `README.md` and `AGENTS.md` up to date with important learnings and changes.
