# STE Retention

This project answers one question. Does a model follow a writing standard
better when you give the standard a name?

## The question

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

The cross is necessary. If you compare a short named prompt against a long
prompt with rules, you cannot tell the two effects apart. The difference could
come from the name. The difference could also come from the length. The cross
separates them.

The experiment gives three results:

- The effect of the name, across both levels of rule detail.
- The effect of the rules, across both levels of naming.
- The interaction. A negative value shows that the two overlap.

### Pairs

One session runs all four variants against one model. All four variants get the
same questions in the same order. This makes each result a paired value.
Differences between models do not add noise. Differences between questions do
not add noise. A paired design needs about half the sessions of an unpaired
design.

### Probes

The script scores compliance at three points in the conversation. The default
points are turn 1, turn 6, and turn 12. The script still sends the turns between
the probes. Those turns build the context. The judge model never reads them.
This keeps the cost low.

### Adaptive control

The script runs in batches. It tests the result after each batch. It stops when
the result is clear. It also stops when the money runs out.

If the deepest probe shows no effect, more sessions will not help. The script
then makes the conversation longer instead. It changes the probes to turn 20,
and then to turn 32.

The script tests the result six times. Repeated tests make a false result more
likely. The script therefore uses a stricter limit of p = 0.0158. This is a
Pocock boundary for six looks at a two-sided 0.05 level.

## Before you start

You need Python 3.9 or later. You need one package:

```
pip install requests
```

You need an OpenRouter key. One key gives access to all the models.

```
export OPENROUTER_API_KEY=sk-or-v1-...
```

## How to run the experiment

1. Open `ste_retention.py` in a text editor.
2. Change the globals at the top of the file if necessary.
3. Run `python ste_retention.py`.
4. Read the cost estimate. Type `y` to start.
5. Run `python make_leaderboard.py` when the experiment stops.
6. Open `leaderboard.html` in a browser.

## The files

### `ste_retention.py`

This file runs the experiment. It also holds all the controls.

**Globals.** The controls are at the top of the file, after the imports. There
is no configuration file. The controls set the model list, the four constraint
strings, the probe depths, the batch size, the budget, and the judge model. Two
flags control the judge. The `PRICING` table gives approximate prices. The
prices are for the cost estimate only. They do not change the experiment.

**Cost estimate.** The script calculates the cost of one batch before it starts.
It asks you to confirm. The estimate includes the input tokens, the output
tokens, and the judge.

**The outer loop.** The loop runs one batch at a time. Each batch runs
`SESSIONS_PER_BATCH` sessions for each model. Each session runs all four
variants.

**One session.** The script sends the constraint in the system prompt. It sends
the constraint one time only. It then asks one question for each turn. It keeps
all replies in the history. It sends the full history with each new question.
Nothing repeats the constraint.

**Scoring.** The script scores a reply at a probe depth only. The first part of
the score is a calculation. The script counts the words in each sentence. It
finds sentences of more than 20 words. It finds the passive voice. It compares
the words against an approved list, if you supply one. The second part of the
score comes from a judge model. The judge reads one passage. The judge does not
know which variant made the passage. The judge does not know the turn number.
The two parts combine into one score from 0 to 100.

**Statistics.** The file includes a t-test. The test does not need SciPy. It
uses an incomplete beta function to find the p-value. This keeps the only
dependency at `requests`.

**Records.** The script writes one line of JSON for each scored reply. Each line
holds the session number, the model, the variant, the depth, the score, the
metrics, the judge value, and the full text. The script writes each line
immediately. A failure does not lose the earlier data.

**Approved words.** The ASD-STE100 dictionary has copyright protection. This
project does not include it. Set `APPROVED_WORDS_FILE` to the path of a word
list if you have one. If you do not set it, the vocabulary part of the score
drops out. The other parts continue to work.

### `make_leaderboard.py`

This file makes the web page. It does not call any model. It reads the records
and writes one HTML file.

**Input.** The script reads `ste_retention_run/records.jsonl`. It groups the
records by session, model, and depth. It keeps a group only when all four
variants are present. An incomplete group would break the pairing.

**Calculation.** The script calculates the three contrasts for each complete
group. It then runs a one-sample t-test on each contrast. It uses the same test
function as the experiment. This makes sure that the two files agree.

**The chart.** The chart is at the top of the page. Each row shows one factor at
one depth. A dot shows the size of the effect. A bar shows the 95 percent
confidence interval. A vertical line marks zero. A bar that crosses the line
shows no reliable effect. The script draws the chart as SVG. It uses no chart
library.

**The tables.** Three tables follow the chart. The first table gives the
numbers behind the chart. The second table gives the mean score for each of the
four variants. The third table gives the naming effect for each model. The third
table is a secondary result. Each model has fewer sessions than the pooled
total.

**Output.** The script writes `leaderboard.html`. The file is complete in
itself. It needs no server. You can put the file on any web host.

### `leaderboard_preview.html`

This file is an example. It shows the layout.

**The data is false.** A script made the data. No model made the data. Do not
use the numbers. Do not publish the file.

The file lets you look at the design before you spend money. You can change the
colors and the layout in the `CSS` string in `make_leaderboard.py`. When you run
the real experiment, the same code writes a new file with true data.

## Cost

The default settings cost about 5.92 USD for each batch. Most runs stop after
two or three batches. A complete run therefore costs about 12 to 18 USD.

Longer probes cost more. The extensions cost about 14.24 USD and 33.64 USD for
each batch. The script shows the new cost when it extends the depth.

## Limits of this work

The score is not a full test of ASD-STE100. Without the approved word list, the
score measures sentence length and the active voice only. The judge model adds
an opinion. The judge is not a certified checker.

The result applies to the models in the list on the day of the run. Models
change. The result does not transfer to other standards without a new
experiment.

The per-model results use no correction for multiple comparisons. Read them as
an indication. Do not read them as a ranking.

## License and data

Publish the raw replies with the results. A leaderboard has value only when
another person can check it.
