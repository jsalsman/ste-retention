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
  // The server's model catalog is the only source of identifiers, labels, prices,
  // and cost anchors. It stays null until a validated `/api/models` response arrives,
  // and cost copy stays neutral while it is missing.
  let catalog = null;
  let controller = null;

  /** Format a validated dollar amount for approximate, non-binding disclosure copy. */
  function dollars(value) {
    // Up to four decimals keep very small paid-model estimates distinguishable from zero.
    const currency = {style:"currency", currency:"USD", minimumFractionDigits:2, maximumFractionDigits:4};
    return value.toLocaleString("en-US", currency);
  }

  /** Render an empirically calibrated planning estimate for the selected workload.
   *
   * The range scales two completed-study observations by logical work and by the
   * selected model's combined base-token price. It remains approximate because
   * model output length, input/output mix, continuations, and routing can differ.
   * Free catalog variants receive rate-limit guidance instead of a dollar range.
   */
  function showCostEstimate(logicalUnits) {
    const estimate = document.querySelector("#cost-estimate");
    const price = catalog?.prices.get(model.value);
    if (!price || !Number.isFinite(logicalUnits)) {
      // Never retain a stale dollar range for an invalid model or workload.
      estimate.textContent = "Enter valid settings to calculate a rough cost range.";
      return;
    }
    const verified = `Prices were checked against OpenRouter on ${catalog.verifiedOn} and can change.`;
    if (price.free) {
      // Zero listed prices still carry provider limits that can interrupt long studies.
      estimate.textContent = `Rough cost estimate: $0.00. This free variant lists no token charges, but OpenRouter rate limits free models, so a long study can stop early; resume it with the run ID. ${verified} Set an OpenRouter spending limit in case pricing or routing changes.`;
      return;
    }
    const {low, high, logicalUnits:referenceUnits} = catalog.calibration;
    const blendedPrice = price.input + price.output;
    // Each anchor is normalized by its reference model's price, then rescaled here.
    const lowCost = logicalUnits * blendedPrice * low.factor;
    const highCost = logicalUnits * blendedPrice * high.factor;
    // Currency formatting is approximate and does not imply a provider-side cap.
    estimate.textContent = `Rough cost range: ${dollars(lowCost)}–${dollars(highCost)}. Calibrated from completed ${referenceUnits}-unit studies that cost ${dollars(low.usd)} with ${low.label} and ${dollars(high.usd)} with ${high.label}, then scaled by selected workload and base token prices. ${verified} Actual input/output mix, continuations, and routing vary; set an OpenRouter spending limit.`;
  }

  /** Validate an untrusted `/api/models` payload into price and calibration lookups.
   *
   * Every identifier, label, price, and anchor is checked before it can reach the
   * menu or an arithmetic expression. Any malformed entry rejects the whole
   * catalog, so the page never shows a partial menu or a misleading estimate.
   */
  function parseCatalog(data) {
    const text = (value) => typeof value === "string" && value.trim() !== "" && value.length <= 200;
    const rate = (value) => Number.isFinite(value) && value >= 0 && value < 1;
    if (!Array.isArray(data?.models) || !data.models.length || !text(data.verified_on)) return null;
    const models = [];
    const prices = new Map();
    for (const entry of data.models) {
      // Reject duplicates and non-numeric or negative prices instead of guessing.
      if (!text(entry?.id) || !text(entry.label) || !rate(entry.input) || !rate(entry.output) ||
          typeof entry.free !== "boolean" || prices.has(entry.id)) return null;
      models.push({id:entry.id, label:entry.label, free:entry.free, input:entry.input, output:entry.output});
      prices.set(entry.id, {input:entry.input, output:entry.output, free:entry.free});
    }
    const units = data.calibration?.logical_units;
    if (!Number.isInteger(units) || units < 1) return null;
    const calibration = {logicalUnits:units};
    for (const bound of ["low", "high"]) {
      const anchor = data.calibration[bound];
      const reference = prices.get(anchor?.model);
      // Anchors must name a priced catalog model so normalization cannot divide by zero.
      if (!reference || !text(anchor.label) || !Number.isFinite(anchor.usd) || anchor.usd <= 0 ||
          reference.input + reference.output <= 0) return null;
      const factor = anchor.usd / units / (reference.input + reference.output);
      calibration[bound] = {label:anchor.label, usd:anchor.usd, factor};
    }
    return {models, prices, calibration, verifiedOn:data.verified_on};
  }

  /** Describe one catalog model with its exact identifier and per-million-token prices.
   *
   * The menu is wide enough for this detail, which lets users confirm the exact
   * upstream identifier and price without leaving the page. Free variants say so
   * instead of listing zero rates, because their label already marks them free.
   */
  function optionText(entry) {
    if (entry.free) return `${entry.label} — ${entry.id}`;
    // Per-million-token rates match OpenRouter's catalog display and avoid tiny decimals.
    const perMillion = (rate) => dollars(rate * 1e6);
    return `${entry.label} — ${entry.id} — ${perMillion(entry.input)} in / ${perMillion(entry.output)} out per 1M tokens`;
  }

  /** Fetch the server catalog, then build grouped model options without HTML parsing.
   *
   * Options use `textContent` so catalog labels stay inert. The first catalog entry
   * remains the default selection. A failure leaves an empty, invalid selection
   * with an announced reload instruction rather than a stale hard-coded menu.
   */
  async function loadModels() {
    const status = document.querySelector("#model-status");
    try {
      const response = await fetch("/api/models", {headers:{"Accept":"application/json"}});
      if (!response.ok) throw new Error("Model catalog lookup failed.");
      catalog = parseCatalog(await response.json());
      if (!catalog) throw new Error("Model catalog is malformed.");
      // Separate paid and free variants so rate-limited choices are easy to recognize.
      const groups = [["Paid models", false], ["Free models (rate limited)", true]];
      const fragment = document.createDocumentFragment();
      for (const [label, free] of groups) {
        const members = catalog.models.filter((entry) => entry.free === free);
        if (!members.length) continue;
        const group = document.createElement("optgroup");
        group.label = label;
        for (const entry of members) {
          const option = document.createElement("option");
          option.value = entry.id;
          option.textContent = optionText(entry);
          group.append(option);
        }
        fragment.append(group);
      }
      model.replaceChildren(fragment);
      // Select the catalog's first entry, even when grouping reorders the menu.
      model.value = catalog.models[0].id;
      status.textContent = "";
    } catch (_error) {
      // An empty value keeps both server validation and the cost estimate neutral.
      catalog = null;
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "Models unavailable";
      model.replaceChildren(option);
      status.textContent = "The model list could not be loaded. Reload the page to try again.";
    }
    // Refresh workload and cost copy for the newly available (or missing) selection.
    showWorkload();
  }

  /** Read the selected catalog model, reporting an empty selection natively. */
  function selectedModel() {
    if (!model.value) {
      // Native validity connects the message to the model menu.
      model.setCustomValidity("Select a model.");
      model.reportValidity();
      throw new Error("A model is required.");
    }
    model.setCustomValidity("");
    return model.value;
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
        body:JSON.stringify({api_key:credential(), model:selectedModel(), prompt:document.querySelector("#prompt").value})});
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
    }
  }

  /** Format validated preview records as inert, readable completion details.
   *
   * Invalid record fields are omitted rather than interpolated into a misleading
   * result. Model-generated response text remains safe because the caller assigns
   * the returned string with `textContent`, never HTML parsing.
   */
  function formatPreviewResults(records) {
    if (!Array.isArray(records)) return "Preview results are unavailable.";
    const labels = {bare:"Bare", rules:"Rules", named:"Named", named_rules:"Named + rules"};
    const sections = [];
    for (const record of records) {
      // Validate every displayed coordinate and score instead of trusting stream data.
      const validNumber = Number.isInteger(record?.session) && Number.isInteger(record?.depth) &&
        Number.isFinite(record?.score) && record.score >= 0 && record.score <= 100;
      if (!validNumber || !labels[record.variant] || typeof record.text !== "string") continue;
      // Plain-text headings keep long model responses distinct and keyboard-readable.
      sections.push(`${labels[record.variant]} — batch ${record.session}, turn ${record.depth} — score ${record.score.toFixed(1)}\n${record.text}`);
    }
    return sections.length ? `Preview results\n\n${sections.join("\n\n")}` : "Preview results are unavailable.";
  }

  /** Apply one validated NDJSON progress object for the mode actually submitted. */
  function showEvent(event, submittedMode) {
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
    // Convert the server's measured seconds to a readable minute estimate at display time.
    const etaMinutes = Number.isFinite(event.eta_seconds) && event.eta_seconds >= 0
      ? (event.eta_seconds / 60).toFixed(1)
      : null;
    // ETA stays textual and approximate; the interface intentionally has no progress bar.
    overlayEta.textContent = etaMinutes === null
      ? "Estimating time remaining…"
      : `Approximately ${etaMinutes} minutes remaining.`;
    if (event.type === "success") {
      // Preview snapshots remain resumable evidence, but only research records are published.
      const destination = submittedMode === "research"
        ? "Open the leaderboard to view this completed research run."
        : `This short preview is not published on the research leaderboard.\n\n${formatPreviewResults(event.records)}`;
      // Keep the durable count and resume handle useful for either experiment mode.
      result.textContent = `Run ID: ${event.run_id}\nCompleted ${event.total} durable work units. ${destination}`;
    }
    if (event.type === "error") throw new Error("Stream reported an error.");
    return event.type === "success" || event.type === "error";
  }

  /** Parse arbitrary stream chunks while retaining the submitted experiment mode. */
  async function consumeStream(response, submittedMode) {
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
      for (const line of lines) if (line.trim()) terminal = showEvent(JSON.parse(line), submittedMode) || terminal;
      if (done) break;
    }
    if (buffer.trim()) terminal = showEvent(JSON.parse(buffer), submittedMode) || terminal;
    if (!terminal) throw new Error("The stream closed before a terminal event.");
  }

  /** Start a bounded stream and always recover controls on every terminal path. */
  async function startExperiment(event) {
    event.preventDefault();
    // Capture mode before any asynchronous work so later UI changes cannot relabel the run.
    const submittedMode = runMode.value;
    controller = new AbortController();
    setRunning(true);
    try {
      const resume = document.querySelector("#resume-run-id").value.trim();
      // The run ID is safe metadata; the credential remains confined to this request body.
      const body = {api_key:credential(), run_mode:submittedMode, model:selectedModel(), resume_run_id:resume || null};
      // Each mode sends only the settings that define that saved run.
      if (submittedMode === "research") body.sessions = Number(document.querySelector("#sessions").value);
      else {
        body.batches = Number(document.querySelector("#batches").value);
        body.turns = Number(document.querySelector("#turns").value);
      }
      const response = await fetch("/api/experiments/stream", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body), signal:controller.signal});
      await consumeStream(response, submittedMode);
    } catch (error) {
      document.querySelector("#experiment-status").textContent = error.name === "AbortError" ? "Experiment cancelled." : "The experiment disconnected or stopped safely.";
    } finally {
      // Keep the masked key available for another request while releasing request state.
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
  // The menu arrives asynchronously; showMode() already rendered neutral cost copy.
  loadModels();
})();
