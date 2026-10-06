# Code grader

Grade a pile of untrusted student or candidate submissions in parallel, each in its own sandbox with no internet access, so a submission that loops forever, peeks at the hidden tests or tries to phone home cannot affect the grader, the other submissions or your network.

```text
1. Grading 6 submissions, each in its own sandbox with no internet access, at most 3 at a time...
   correct.py        100/100  passed     4.5s
   cheater.py          0/100  blocked    7.5s  blocked: access to the grader's files (test_top_words.py)
   infinite_loop.py   83/100  timeout    7.4s  1 test(s) hit the 3s limit
   partial.py         67/100  partial    4.7s  failed: test_punctuation_separates_words, test_ties_break_alphabetically
   wrong.py            0/100  failed     4.7s  failed all 6 tests
   exfiltrator.py      0/100  blocked   21.7s  blocked: access to the grader's files (test_top_words.py); network access to collector.example.net
2. Asking the model for one hint per failing submission...
   infinite_loop.py: The while loop never increments `i` for punctuation characters, causing an infinite loop.
   partial.py: Use regex to extract words; `split` leaves punctuation. Sort by `(-count, word)` for ties.
   wrong.py: The function returns word-count pairs instead of just the words.
3. Checking the grades against the answer key (expected.json)...
   all 6 match
4. Wrote results.json
Done: 6 submissions graded, all match the key, 6 sandboxes created and deleted.
```

## What you need

- Python 3.11 or later
- A NeevCloud account with an API key from **Account > API Keys** ([how to create one](https://docs.ai.neevcloud.com/getting-started/create-api-key)) with Resource Type **Sandboxes** (`NEEV_API_KEY`)
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)
- Only for `--feedback`: a second key with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python grader.py              # add --feedback (and NEEV_MODEL_API_KEY) for a hint per failing submission
```

On Windows, use the PowerShell setup in [Setting up a recipe](../../README.md#setting-up-a-recipe) for the virtualenv and the keys.

The grades go to `results.json` (change it with `--out`). The script exits 0 only when every submission was graded, every grade matches the answer key in `expected.json`, and every sandbox was deleted.

## What is in the folder

- `assignment/problem.md` is what students see: write `top_words(text, k)`.
- `assignment/test_top_words.py` is the hidden unittest suite. It never leaves the grader except into a directory the submission cannot read.
- `submissions/` holds six samples: `correct.py`, `partial.py` (splits on spaces and ignores ties), `wrong.py` (returns `(word, count)` pairs), `infinite_loop.py` (hangs on punctuation), `cheater.py` (tries to read and rewrite the hidden tests, patch `assertEqual` and print a fake result) and `exfiltrator.py` (solves the task but first tries to post the environment and the tests to an outside server).
- `harness.py` runs inside each sandbox: it runs the hidden tests and calls the submission for them.

## How it works

1. A pool of three workers takes the submissions. For each one, `client.sandboxes.create({"name": "grader-<random>", "egress": {"mode": "deny_all"}})` starts a fresh Linux machine with no internet access, so nothing a submission does can reach the network or another submission.
2. `sandbox.files.write` uploads the submission to `submission/solution.py`, then the harness and the hidden tests to `grader/`. The tests go in last, right before the run, and none of the submission's code has run yet.
3. One `sandbox.exec(["sh", "-c", "chmod 700 grader && exec python3 -I grader/harness.py"], stdin=..., timeout_ms=60000)` makes `grader/` readable by root only and runs the tests as root. The submission never runs in the tests' process: for every call the tests make, the harness starts a fresh process as the unprivileged `nobody` user, with limits on processes, memory and file size, sends it the arguments and reads back the return value. A call that takes more than 3 seconds is killed along with everything it started, and that test fails as a timeout while the others still count.
4. That design is what makes cheating pointless. The submission cannot read or change the tests (`Permission denied`), patching `unittest` only changes its own process, and anything it prints goes to its own captured output. The harness prints its result on one line tagged with a random token the grader passed on stdin for this run only, and the grader ignores every other line.
5. The grader turns the report into a score out of 100 and an outcome (`passed`, `partial`, `failed`, `timeout` or `blocked`), deletes the sandbox, prints the line, and starts the next submission. At the end it checks every grade against `expected.json` and writes `results.json`.

The isolation is enforced by the sandbox and by file permissions. The `blocked` outcome is a report on top: a Python audit hook in the submission's process notes any reach for the network or the grader's files, and the grader gives such a submission 0 for review. A submission that hides its attempt from the hook still gets nothing out; it just is not flagged.

With `--feedback`, each `partial`, `failed` or `timeout` submission gets one short hint from a model on NeevCloud (`glm-4-7` by default; set `MODEL` to use another, for example `MODEL=glm-5-2`). The model sees the problem, the submission and the names of the tests it failed, never the tests themselves, and its answer never changes a grade.

To grade your own assignment, replace `assignment/` (keep the tests calling the submission through `from harness import submission`), put the submissions in `submissions/`, and list the grades you expect in `expected.json`.

## Time and cost

About 25 seconds end to end for the six samples, plus about 25 seconds for `--feedback`. Most submissions take 4 to 8 seconds each including creating the sandbox; `exfiltrator.py` takes about 22 seconds because every one of its six calls waits out the 3-second limit while its network attempt hangs. At most three sandboxes run at once and each lives only while its submission is graded. You pay for those sandbox seconds and, with `--feedback`, for a few hundred model tokens per hint.

## Cleanup

Each sandbox is deleted as soon as its submission is graded, and every sandbox still running is deleted when the script fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `grader-` sandbox from the console.
