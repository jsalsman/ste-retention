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
  let controller = null;

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
    // State the paid workload before the user submits the form.
    document.querySelector("#mode-help").textContent = research
      ? "The full protocol is resumable and can take longer than the web request limit. Experiments use the predefined prompt pool, not text entered under “Try one prompt.”"
      : "The preview uses all four variants and makes at most 12 calls. Experiments use the predefined prompt pool, not text entered under “Try one prompt.”";
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
    if (event.type === "success") result.textContent = `Run ID: ${event.run_id}\nCompleted ${event.total} durable work units. Open the leaderboard to view completed runs.`;
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
  cancel.addEventListener("click", () => controller?.abort());
  checkRun.addEventListener("click", checkRunStatus);
  showMode();
})();
