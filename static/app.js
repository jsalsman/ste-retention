document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("experiment-form");
    const runBtn = document.getElementById("run-btn");
    const apiKeyInput = document.getElementById("api-key");
    const modelSelect = document.getElementById("model");

    const overlay = document.getElementById("loading-overlay");
    const statusMessage = document.getElementById("status-message");
    const etaMessage = document.getElementById("eta-message");
    const cancelBtn = document.getElementById("cancel-btn");

    const leaderboardContainer = document.getElementById("leaderboard-container");

    let abortController = null;

    // Load initial leaderboard
    fetchLeaderboard();

    function fetchLeaderboard() {
        fetch("/api/leaderboard")
            .then(res => res.text())
            .then(html => {
                leaderboardContainer.innerHTML = html;
            })
            .catch(err => {
                leaderboardContainer.textContent = "Error loading leaderboard.";
            });
    }

    form.addEventListener("submit", async (e) => {
        e.preventDefault();

        const apiKey = apiKeyInput.value.trim();
        const model = modelSelect.value;

        if (!apiKey) return;

        // Reset UI state
        runBtn.disabled = true;
        overlay.classList.remove("hidden");
        statusMessage.textContent = "Starting bounded experiment...";
        etaMessage.textContent = "";

        abortController = new AbortController();

        try {
            const response = await fetch("/api/experiment/stream", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ api_key: apiKey, model: model }),
                signal: abortController.signal
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || "Failed to start experiment");
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");

                // Keep the last incomplete line in the buffer
                buffer = lines.pop();

                for (const line of lines) {
                    if (!line.trim()) continue;

                    try {
                        const event = JSON.parse(line);
                        handleEvent(event);
                    } catch (err) {
                        console.error("Failed to parse event", err);
                    }
                }
            }

            // Clean up any remaining buffer
            if (buffer.trim()) {
                try {
                    const event = JSON.parse(buffer);
                    handleEvent(event);
                } catch (err) {
                    console.error("Failed to parse event", err);
                }
            }

            if (runBtn.disabled && !overlay.classList.contains("hidden") && statusMessage.textContent !== "Experiment cancelled.") {
                // Stream closed without a terminal event
                statusMessage.textContent = "Error: Stream closed unexpectedly.";
                etaMessage.textContent = "";
                finishRun();
            }

        } catch (error) {
            if (error.name === "AbortError") {
                statusMessage.textContent = "Experiment cancelled.";
            } else {
                statusMessage.textContent = `Error: ${error.message}`;
            }
            etaMessage.textContent = "";
            finishRun();
        }
    });

    cancelBtn.addEventListener("click", () => {
        if (abortController) {
            abortController.abort();
            abortController = null;
        }
    });

    function handleEvent(event) {
        if (event.type === "start" || event.type === "status") {
            statusMessage.textContent = event.message;
            if (event.eta) {
                etaMessage.textContent = event.eta;
            } else {
                etaMessage.textContent = "";
            }
        } else if (event.type === "success") {
            statusMessage.textContent = event.message;
            etaMessage.textContent = "";
            fetchLeaderboard();
            finishRun(true);
        } else if (event.type === "error") {
            statusMessage.textContent = event.message;
            etaMessage.textContent = "";
            finishRun();
        }
    }

    function finishRun(success = false) {
        // Clear API key on terminal outcome
        apiKeyInput.value = "";

        setTimeout(() => {
            overlay.classList.add("hidden");
            runBtn.disabled = false;
        }, success ? 2000 : 4000); // Give user time to read the success/error message
    }
});
