/** Browser controller for credential-safe interactions and streamed experiments. */
(() => {
  "use strict";
  // Cached controls are never used to preserve the key beyond an active request.
  const key = document.querySelector("#api-key");
  const model = document.querySelector("#model");
  const chatForm = document.querySelector("#chat-form");
  const runForm = document.querySelector("#experiment-form");
  const runMode = document.querySelector("#run-mode");
  const overlay = document.querySelector("#loading-overlay");
  const overlayMessage = document.querySelector("#overlay-message");
  const overlayEta = document.querySelector("#overlay-eta");
  const cancel = document.querySelector("#cancel");
  const checkRun = document.querySelector("#check-run");
  const start = runForm.querySelector("button[type='submit']");
  const sessions = document.querySelector("#sessions");
  // Workload factors mirror the four server variants, deepest research turn,
  // and one initial provider request plus three bounded continuations per unit.
  const VARIANT_COUNT = 4;
  const RESEARCH_TURNS = 12;
  const REQUESTS_PER_UNIT = 4;
  // Per-token OpenRouter prices were verified from the live catalog on 2026-09-25.
  // Estimates use these exact-model input/output rates and never fetch with credentials.
  const MODEL_PRICES = Object.freeze({
    "google/gemini-3.8-flash": {input: 0.00000075, output: 0.00000375},
    "openai/gpt-6-sol": {input: 0.000002, output: 0.00001},
    "anthropic/claude-sonnet-5": {input: 0.000002, output: 0.00001},
    "meta-llama/llama-4-maverick": {input: 0.0000001875, output: 0.0000006525},
  });
  let controller = null;

  /** Render a deliberately broad planning estimate for the selected workload.
   *
   * The low end assumes one request with 500 input and 300 output tokens per
   * logical unit. The high end assumes all four requests, each with 8,000 input
   * and 8,192 output tokens. Actual context, output, and provider routing vary.
   */
  function showCostEstimate(logicalUnits) {
    const estimate = document.querySelector("#cost-estimate");
    const price = MODEL_PRICES[model.value];
    if (!price || !Number.isFinite(logicalUnits)) {
      // Never retain a stale dollar range for an invalid model or workload.
      estimate.textContent = "Enter valid settings to calculate a rough cost range.";
      return;
    }
    const low = logicalUnits * (500 * price.input + 300 * price.output);
    const high = logicalUnits * REQUESTS_PER_UNIT * (8000 * price.input + 8192 * price.output);
    // Currency formatting is approximate and does not imply a provider-side cap.
    const currency = {style:"currency", currency:"USD", minimumFractionDigits:2, maximumFractionDigits:4};
    estimate.textContent = `Rough cost range: ${low.toLocaleString("en-US", currency)}–${high.toLocaleString("en-US", currency)}. Assumes 500 input + 300 output tokens per unit at the low end, and four requests of 8,000 input + 8,192 output tokens per unit at the high end. Actual token use and routing vary; set an OpenRouter spending limit.`;
  }

  /** Recalculate the visible workload from the currently selected form values.
   *
   * Research counts use every turn through the deepest probe because intermediate
   * turns build context. Preview counts use selected batches and turns. Invalid
   * in-progress input receives a neutral prompt until native validation can run.
   */
  function showWorkload() {
    const help = document.querySelector("#mode-help");
    const research = runMode.value === "research";
    // Read only the active mode's numeric controls so hidden values cannot confuse copy.
    const primary = research ? Number(sessions.value) : Number(document.querySelector("#batches").value);
    const turns = research ? RESEARCH_TURNS : Number(document.querySelector("#turns").value);
    const primaryMaximum = research ? Number(sessions.max) : Number(document.querySelector("#batches").max);
    const turnsMaximum = research ? RESEARCH_TURNS : Number(document.querySelector("#turns").max);
    if (!Number.isInteger(primary) || primary < 1 || primary > primaryMaximum ||
        !Number.isInteger(turns) || turns < 1 || turns > turnsMaximum) {
      // Do not advertise a stale workload while the user edits a numeric field.
      help.textContent = research
        ? `Enter 1 through ${primaryMaximum.toLocaleString("en-US")} research sessions to stay within the paid-request safety ceiling.`
        : "Enter valid preview settings to calculate the maximum paid workload.";
      showCostEstimate(Number.NaN);
      return;
    }
    const logicalUnits = primary * turns * VARIANT_COUNT;
    const maximumRequests = logicalUnits * REQUESTS_PER_UNIT;
    // Cost guidance uses the same selected logical workload as paid-request copy.
    showCostEstimate(logicalUnits);
    // Locale formatting makes large selected workloads legible without changing values.
    const logicalText = logicalUnits.toLocaleString("en-US");
    const requestText = maximumRequests.toLocaleString("en-US");
    help.textContent = research
      ? `The full protocol is resumable and can take longer than the web request limit. ${primary.toLocaleString("en-US")} selected sessions use ${logicalText} logical generations and at most ${requestText} paid provider requests. Experiments use the predefined prompt pool, not text entered under “Try one prompt.”`
      : `The preview uses all four variants, ${logicalText} logical generations, and at most ${requestText} paid provider requests. Experiments use the predefined prompt pool, not text entered under “Try one prompt.”`;
  }

  /** Show only the controls that apply to the selected experiment protocol. */
  function showMode() {
    const research = runMode.value === "research";
    const previewOptions = document.querySelector("#preview-options");
    const researchOptions = document.querySelector("#research-options");
    // Hidden controls are also disabled so browser validation ignores them.
    previewOptions.hidden = research;
    researchOptions.hidden = !research;
    for (const input of previewOptions.querySelectorAll("input")) input.disabled = research;
    for (const input of researchOptions.querySelectorAll("input")) input.disabled = !research;
    // Refresh paid-work disclosure whenever a mode change activates different inputs.
    showWorkload();
  }

  /** Read and validate the shared credential without copying it into browser storage. */
  function credential() {
    if (!key.value.trim()) {
      // Native validity connects the message to the password input.
      key.setCustomValidity("Enter an OpenRouter API key.");
      key.reportValidity();
      throw new Error("An API key is required.");
    }
    key.setCustomValidity("");
    return key.value.trim();
  }

  /** Toggle experiment controls and the modal-looking, non-focus-trapping overlay. */
  function setRunning(running, message = "Starting the experiment…") {
    overlay.hidden = !running;
    overlay.setAttribute("aria-hidden", String(!running));
    overlayMessage.textContent = message;
    overlayEta.textContent = "";
    cancel.disabled = !running;
    // Preserve cancellation access while preventing duplicate submissions.
    for (const element of runForm.querySelectorAll("input, button[type='submit']")) element.disabled = running;
    // Move keyboard focus into the visible overlay, then restore mode-specific controls.
    if (running) cancel.focus();
    else {
      // The broad running-state toggle enabled every input, including hidden required fields.
      showMode();
      start.focus();
    }
  }

  /** Submit one non-streamed interaction and render server text without HTML parsing. */
  async function askModel(event) {
    event.preventDefault();
    const status = document.querySelector("#chat-status");
    const result = document.querySelector("#chat-result");
    status.textContent = "Contacting the model…";
    try {
      const response = await fetch("/api/interact", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({api_key:credential(), model:model.value, prompt:document.querySelector("#prompt").value})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "The request failed.");
      // textContent makes model-generated markup inert.
      result.textContent = data.answer;
      // Explicit completion status prevents truncated text from appearing definitive.
      status.textContent = data.complete
        ? "Response received."
        : "Incomplete response received; the displayed text may be truncated.";
    } catch (error) {
      status.textContent = error.name === "AbortError" ? "Request cancelled." : "The request could not be completed.";
      result.textContent = "No response is available.";
    } finally {
      key.value = "";
    }
  }

  /** Apply one validated NDJSON progress object to accessible status regions. */
  function showEvent(event) {
    const status = document.querySelector("#experiment-status");
    const resume = document.querySelector("#resume-run-id");
    const result = document.querySelector("#experiment-result");
    status.textContent = event.message || "Experiment update received.";
    overlayMessage.textContent = status.textContent;
    if (typeof event.run_id === "string") {
      // Keep the safe identifier visible so a refresh or disconnect can be resumed manually.
      resume.value = event.run_id;
      result.textContent = `Run ID: ${event.run_id}`;
    }
    // ETA stays textual and approximate; the interface intentionally has no progress bar.
    overlayEta.textContent = Number.isFinite(event.eta_seconds) ? `Approximately ${event.eta_seconds} seconds remaining.` : "Estimating time remaining…";
    if (event.type === "success") {
      // Preview snapshots remain resumable evidence, but only research records are published.
      const destination = runMode.value === "research"
        ? "Open the leaderboard to view this completed research run."
        : "This short preview is not published on the research leaderboard.";
      // Keep the durable count and resume handle useful for either experiment mode.
      result.textContent = `Run ID: ${event.run_id}\nCompleted ${event.total} durable work units. ${destination}`;
    }
    if (event.type === "error") throw new Error("Stream reported an error.");
    return event.type === "success" || event.type === "error";
  }

  /** Parse arbitrary stream chunks with a retained partial-line buffer. */
  async function consumeStream(response) {
    if (!response.ok || !response.body) throw new Error("Streaming is unavailable.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminal = false;
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream:!done});
      // Keep the final incomplete record until the next network chunk.
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) if (line.trim()) terminal = showEvent(JSON.parse(line)) || terminal;
      if (done) break;
    }
    if (buffer.trim()) terminal = showEvent(JSON.parse(buffer)) || terminal;
    if (!terminal) throw new Error("The stream closed before a terminal event.");
  }

  /** Start a bounded stream and always recover controls on every terminal path. */
  async function startExperiment(event) {
    event.preventDefault();
    controller = new AbortController();
    setRunning(true);
    try {
      const resume = document.querySelector("#resume-run-id").value.trim();
      // The run ID is safe metadata; the credential remains confined to this request body.
      const body = {api_key:credential(), run_mode:runMode.value, model:model.value, resume_run_id:resume || null};
      // Each mode sends only the settings that define that saved run.
      if (runMode.value === "research") body.sessions = Number(document.querySelector("#sessions").value);
      else {
        body.batches = Number(document.querySelector("#batches").value);
        body.turns = Number(document.querySelector("#turns").value);
      }
      const response = await fetch("/api/experiments/stream", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body), signal:controller.signal});
      await consumeStream(response);
    } catch (error) {
      document.querySelector("#experiment-status").textContent = error.name === "AbortError" ? "Experiment cancelled." : "The experiment disconnected or stopped safely.";
    } finally {
      // Clearing both references minimizes accidental credential retention.
      key.value = "";
      controller = null;
      setRunning(false);
    }
  }

  /** Query safe heartbeat metadata and announce whether a persisted run is active or stalled. */
  async function checkRunStatus() {
    const runId = document.querySelector("#resume-run-id").value.trim();
    const status = document.querySelector("#experiment-status");
    if (!/^[a-f0-9]{32}$/.test(runId)) {
      // Reject malformed identifiers before constructing a URL path.
      status.textContent = "Enter a valid 32-character run ID first.";
      return;
    }
    status.textContent = "Checking the saved run heartbeat…";
    try {
      const response = await fetch(`/api/experiments/${encodeURIComponent(runId)}/status`, {headers:{"Accept":"application/json"}});
      const data = await response.json();
      if (!response.ok) throw new Error("Status lookup failed.");
      // Only fixed server status and numeric counts are rendered, always through textContent.
      status.textContent = `Run is ${data.liveness}; ${data.completed} of ${data.total} work units are durable.`;
    } catch (_error) {
      status.textContent = "The saved run status is unavailable.";
    }
  }

  chatForm.addEventListener("submit", askModel);
  runForm.addEventListener("submit", startExperiment);
  runMode.addEventListener("change", showMode);
  model.addEventListener("change", showWorkload);
  // Every workload-defining input updates the disclosure before paid work can start.
  for (const input of runForm.querySelectorAll("#sessions, #batches, #turns")) {
    input.addEventListener("input", showWorkload);
  }
  cancel.addEventListener("click", () => controller?.abort());
  checkRun.addEventListener("click", checkRunStatus);
  showMode();
})();
