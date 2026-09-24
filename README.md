# STE Retention

This project answers one question. Does a model follow a writing standard better when you give the standard a name?

The project tests ASD-STE100 Simplified Technical English (STE). The project is a research tool. It is not an STE checker. It does not give STE certification.

## The question

You can ask a model to use a controlled writing style in two ways. You can name the standard. You can also describe selected rules.

The two methods ask for the same type of output. However, they can have different effects.

A name can work as a key. The model can use the key to find information that it learned about the standard. A description has no such key. The model reads the description as a set of direct instructions.

This project measures the difference between the two methods. It also measures how the difference changes as the conversation becomes longer.

## The design

The experiment crosses two factors. One factor is the name of the standard. The other factor is the list of rules. The two factors make four prompt variants.

| Variant | Names the standard | Gives the rules |
|---|---:|---:|
| `bare` | No | No |
| `named` | Yes | No |
| `rules` | No | Yes |
| `named_rules` | Yes | Yes |

This cross is necessary. A comparison of a short named prompt with a long rule prompt cannot separate the two effects. The difference can come from the name. The difference can also come from the rule detail or prompt length.

The experiment gives three results:

* The name effect across both levels of rule detail.
* The rule effect across both levels of naming.
* The interaction between the name and the rules.

The code calculates these paired contrasts:

* Rule effect: `((rules - bare) + (named_rules - named)) / 2`
* Name effect: `((named - bare) + (named_rules - rules)) / 2`
* Interaction: `named_rules - named - rules + bare`

A negative interaction can show that the name and the rules overlap. The interaction does not, by itself, identify the cause of the overlap.

### Pairs

One session runs all four variants against one model. All four variants get the same questions in the same sequence. This procedure gives paired observations.

The stored seed and the session identity determine the sequence. A session does not repeat a question. Saved replies rebuild the same conversation when a run resumes.

The leaderboard compares results only within the same run, model, session, depth, protocol version, and scoring version. It rejects an incomplete four-variant cell. Thus, results from different questions or incompatible versions do not enter one pair.

### Probes

The full study scores compliance at selected conversation depths. The default depths are turns 1, 6, and 12.

The worker still sends the turns between the probes. These turns build the conversation context. The worker keeps the complete history for each variant. It scores and records only the selected probe turns.

The web preview is different. It scores each requested turn and permits only one to three turns. Use the preview to examine the workflow. Do not combine preview results with research results.

### Study size

The full worker uses this generation-call formula:

```text
models × sessions × 4 variants × deepest probe
```

The default web study uses one model, six sessions, four variants, and a deepest probe of turn 12. Thus, it makes 288 generation calls.

An optional judge adds this number of calls:

```text
models × sessions × 4 variants × number of probe depths
```

The web form does not use a judge or an approved-word file. The command-line worker can use both options.

### Adaptive control

The first historical program used batches and repeated statistical tests. It could stop when the result was clear or when the budget was exhausted. It could also extend the probes to turns 20 and 32.

The current `ste.research` worker does not implement this adaptive procedure. It runs the models, sessions, and depths that the operator specifies. The `budget_usd` value is stored with the configuration, but it does not stop provider charges.

This difference is deliberate. The current worker does not claim that fixed settings implement the historical Pocock stopping rule.

## Before you start

Use CPython 3.14.7 or a later compatible version. Create a virtual environment and install the development requirements.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

You need an OpenRouter API key for model calls. The web form sends the key in one request. The command-line worker reads the key from its process environment.

```sh
export OPENROUTER_API_KEY='sk-or-v1-...'
```

The application does not save or log the key. The person who supplies the key is responsible for all charges.

## How to get the dictionary

The optional vocabulary score needs a list of approved words. The standard contains the dictionary. This project does not supply the standard or the dictionary.

### Procedure

1. Open the [ASD-STE100 downloads page](https://www.asd-ste100.org/STE_downloads.html).
2. Follow the ASD instructions to request an official copy.
3. Read the license terms that come with the copy.
4. Find Part 2, the dictionary.
5. Make a text file only if your license terms permit this use.
6. Put one approved word on each line.
7. Use lower-case letters.
8. Do not include entries that the standard does not approve.
9. Save the file outside the repository.
10. Pass the path to `python -m ste.research` with `--approved-words`.

Do not commit or publish this word list. The command-line worker reads the file at run time. The worker does not copy the list into a snapshot.

### Two cautions

The standard permits applicable technical names and technical verbs. A plain dictionary list cannot identify these project-specific terms. Thus, the vocabulary score can mark a permitted term as unapproved.

The standard is a specification, not only a word list. The current mechanical score tests a small set of properties. It does not test all rules in ASD-STE100.

## How to run the experiment

### Web application

Start the development server.

```sh
./devserver.sh
```

Open the root page and enter an OpenRouter key. Select a model and an experiment type.

For a single interaction, the server validates both the returned message text and
the provider's finish reason. A token-limit finish returns the available text as
plain text and labels it incomplete, so the browser warns that the displayed text
may be truncated. Malformed provider metadata and provider failures produce a
sanitized error without returning credentials or the provider response body.
Preview and research runners likewise unwrap validated completions to plain text;
they checkpoint safe partial text when a token limit ends a generation.

The model menu currently offers Gemini 3.8 Flash, GPT-6 Sol, Claude Sonnet 5, and
Llama 4 Maverick. These exact OpenRouter model identifiers were verified against
the provider catalog on 2026-09-24. Exact identifiers make a study more
reproducible than moving `latest` aliases, but the catalog can change. Verify
availability before you start a large study.

* **Short preview:** This mode uses all four variants. It makes a maximum of 12 generation calls.
* **Full research study:** This mode uses the selected model and the full `ste.research` protocol. Six sessions make 288 generation calls.

The server sends newline-delimited JSON (NDJSON). Each line is one valid JSON object. The server saves each paid response before it reports completion.

A browser, proxy, or server timeout can stop a full study. Streaming does not extend the Cloud Run request limit. Copy the run ID. Select the same mode and settings, enter the run ID, and supply a new API key to resume.

The web application has no authentication or authorization layer. A person who knows a run ID can inspect status, resume the run, or delete the snapshot. Deletion returns a conflict while that run holds an active lease, which prevents a later checkpoint from recreating the deleted snapshot. Use a suitable deployment boundary if model text is sensitive.

### Command-line worker

This command runs two models and uses the optional word list and judge:

```sh
python -m ste.research \
  --models google/gemini-3.8-flash openai/gpt-6-sol \
  --sessions 6 --depths 1 6 12 --seed 20260916 \
  --budget-usd 40 --provider-timeout 120 \
  --approved-words /secure/approved_words.txt \
  --judge-model anthropic/claude-sonnet-5 --judge-timeout 120 \
  --state /durable/research/RUN.json --yes
```

The command shows 576 generation calls and 144 judge calls for this example. The `--yes` option confirms the displayed workload. It does not confirm a price estimate.

To resume, use the same state path and all the same configuration values. Add `--run-id` with the saved run ID. The worker rejects a changed configuration before it makes a new paid call.

## Cost

The current code does not contain a pricing table. It does not calculate a currency estimate because provider prices and model identifiers can change.

The old program estimated 5.92 USD for one default batch. It estimated 12 to 18 USD for a typical adaptive run. These historical values do not describe the current fixed-session worker. Do not use them as a current quote.

Before a run, calculate the call count from the formulas in the design section. Check the current prices for each selected model in OpenRouter. Include the growing conversation history, output-token limit, and optional judge calls in your estimate.

The CLI `--budget-usd` option records the operator's approved value. The application does not enforce this value against live provider charges. Set a provider-side spending limit at or below the approved amount.

The web form shows the default generation-call count before a full run. It does not show a currency estimate. The user who enters the OpenRouter key accepts the provider charges.

## The files

### `ste/research.py`

This module runs the full experiment. It also supplies the `python -m ste.research` command.

**Configuration.** Command options set the models, sessions, probe depths, seed, timeouts, budget value, optional word list, optional judge, state path, and resume ID.

**Workload confirmation.** The module calculates generation and judge call counts. It prints the counts and the configured budget value. It requires `--yes` before paid work starts.

**One session.** The module sends one system instruction for each variant. It keeps all user and assistant messages in that variant's history. It sends the same prompt sequence to all four variants.

**Checkpoints.** The module saves each generation call and each judge call before it reports the unit. It uses stable unit identities. A resumed run skips saved units and reconstructs the missing conversation history.

**Records.** The module creates one analytical record at each probe depth. Records contain schema, protocol, and scoring versions. They also contain stable run, session, prompt, model, variant, and depth values.

### `ste/experiment.py`

This module runs the short web preview. It limits synchronous work to one batch, three turns, and 12 units. It stops before the deployment request limit and uses short provider timeouts.

Preview records have `run_mode` set to `preview`. The normal leaderboard does not combine them with research data.

### `ste/scoring.py`

This module calculates sentence-length and approximate active-voice components. It can add vocabulary and judge components when the caller supplies them.

The composite score is the arithmetic mean of available component percentages. A missing optional component has a null value. It is not a pass or a failure.

The sentence-length component uses a 25-word ceiling. The output also includes the historical diagnostic for sentences of more than 20 words.

### `ste/statistics.py`

This module supplies a dependency-free paired t-test. It calculates the t value, degrees of freedom, two-sided p value, mean effect, effect size, and confidence-interval half-width.

The current research runner does not use this test for adaptive stopping. The module remains available for analysis.

### `ste/leaderboard.py`

This module renders complete research snapshots. It calculates the rule, name, and interaction contrasts for each complete paired cell.

The current page is a table. It shows the model, protocol version, scoring version, bare baseline, three mean contrasts, and paired-observation count. It does not make the historical SVG confidence-interval chart.

The Flask route reads complete research snapshots from `EXPERIMENTS_DIR`. It can also read the original JSONL format as a migration path.

### Web and support files

* `flask-app.py` supplies HTTP routes, status checks, fenced leases, and NDJSON streams.
* `index.html` is the stand-alone application document.
* `static/app.js` supplies browser behavior.
* `static/styles.css` supplies presentation.
* `ste/protocol.py` supplies prompts, variants, limits, and version values.
* `ste/runs/` supplies snapshots, backend detection, and conditional object leases.
* `ste/records.py` validates record input and the legacy format.
* `originals/` contains historical reference programs. The package build and Ruff exclude these files.

## Scoring limits

The score is not a full test of ASD-STE100. Without an approved-word list, the mechanical score measures sentence length and approximate active voice. The optional judge adds a model opinion. The judge is not a certified checker.

Sentence splitting and passive-voice detection use regular expressions. They can give false positive and false negative results. Technical terms can cause false vocabulary failures.

The result applies to the tested model version on the test date. Models can change. The result does not automatically apply to a different model or writing standard.

Per-model results have fewer observations than combined results. Do not use a small per-model result as a general model rank.

## The standard

ASD owns ASD-STE100 and its dictionary. Get an official copy from the [ASD-STE100 downloads page](https://www.asd-ste100.org/STE_downloads.html).

Do not reprint or change the standard. Do not use ASD marks in a way that implies approval. This project does not imply that ASD approves or certifies the software.

The STEMG has published work about ASD-STE100 and artificial intelligence. See the [ASD-STE100 and AI white paper](https://www.asd-ste100.org/assets/files/WhitePaper-ASD-STE100_and_AI.pdf).

If you publish experiment results, give the protocol and model date. Publish model replies when your data policy permits this. Do not publish the approved-word file.

## Checkpoints, storage, and recovery

The application stores snapshots in `EXPERIMENTS_DIR`. The default path is `/experiments`. A research snapshot contains prompts and model replies that are necessary for resume. It never contains the API key.

The service detects the Cloud Storage mount automatically. It coordinates leases and snapshot fencing through conditional object writes. Multiple Cloud Run instances are safe. The web service has no local-filesystem coordination mode.

The web service reads resumed snapshots through the Cloud Storage API. This avoids stale mount-cache data. Snapshot generation preconditions prevent a request that lost its lease from overwriting newer work.

The `ste.research` CLI with `--state` remains file-based and takes no lease. Do not point it at a run that the web service might run at the same time.

The leaderboard reads only complete research snapshots. It excludes preview and incomplete research runs. It separates records with different protocol and scoring versions.

## Browser behavior and accessibility

During a request, the page shows `static/loading.gif` in an accessible overlay. The page gives text status. It shows an approximate ETA only after sufficient measured work.

The page has no percentage display or progress bar. It restores controls after success, failure, cancellation, disconnection, or an early stream closure.

The browser inserts external values with `textContent`. The leaderboard escapes external labels and validates numeric values. The interface supports keyboard use, live status, contrast, and reduced-motion preferences.

## Development checks

Run these checks before you commit a change:

```sh
pytest
ruff check .
ruff format --check .
python -m compileall -q flask-app.py ste tests
```

Tests replace paid and external calls with test functions. Tests do not need a provider key, network access, or a Docker daemon.

Runtime packages are in `requirements.txt`. Development and test packages are in `requirements-dev.txt`.

## Deployment and data care

The Dockerfile is the Cloud Build contract. The container runs Gunicorn as a non-root user. Mount durable storage at `/experiments`, or set `EXPERIMENTS_DIR` to a durable path.

Snapshots contain user prompts and model replies. Encrypt storage and backups. Limit operator access. Set and disclose a retention period. Delete expired snapshots and lease files with a scheduled job.

The web service requires a Cloud Storage volume at `EXPERIMENTS_DIR` in every environment. It refuses lease work without that mount. Application Default Credentials and the service identity's bucket role authorize conditional object operations. No key file is needed.

Do not put keys, request headers, prompts, replies, provider bodies, or licensed words in logs. Logs can contain safe run identifiers, unit identifiers, status values, and timing values.

A full web study can exceed the deployment request limit. Resume after a timeout. For unattended work, run `python -m ste.research` in a Cloud Run Job.

## License

See `LICENSE` for the software license. ASD-STE100 remains the property of ASD. This project gives no ASD approval or certification.
