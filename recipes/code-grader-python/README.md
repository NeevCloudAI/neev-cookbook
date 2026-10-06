# Code grader

Grade a pile of untrusted student or candidate submissions in parallel, each in its own sandbox with no internet access. A submission that loops forever, peeks at the hidden tests or tries to phone home can't affect the grader, the other submissions or your network.

<p align="center">
  <img src="../../assets/runs/code-grader-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project). A **Model API** key is needed only for `--feedback`.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python grader.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

Add `--feedback` (with `NEEV_MODEL_API_KEY` set) for one hint per failing submission. Grades go to `results.json` (change it with `--out`). The script exits 0 only when every submission was graded, every grade matches `expected.json`, and every sandbox was deleted.

The six sample submissions in `submissions/` cover the cases that matter: a correct one, a partial one, a wrong one, an infinite loop, a cheater that tries to read and rewrite the hidden tests, and one that tries to send the tests to an outside server.

## How it works

1. **One sandbox each.** Three workers take submissions in turn. Each submission gets a fresh sandbox with no internet access.
2. **Hidden tests last.** The submission is uploaded first, then the harness and the hidden tests, into a folder only root can read.
3. **Run, isolated.** The tests run as root. For every call they make, the harness starts the submission in a fresh process as the unprivileged `nobody` user, with limits on processes, memory and file size, and a 3-second limit per call.
4. **Report.** The harness reports its result on one line tagged with a random token for this run only; anything else the submission prints is ignored. So cheating can't change the score.
5. **Grade.** The grader turns the result into a score out of 100 and an outcome: `passed`, `partial`, `failed`, `timeout` or `blocked`. It deletes the sandbox and moves on.

## Use it in your product

- **Your own assignment:** replace `assignment/` with your problem and hidden tests, keeping the tests calling the submission through `from harness import submission`. Put submissions in `submissions/` and the grades you expect in `expected.json`.
- **A grading service:** call the grader from your course platform or hiring tool for each new submission, and store `results.json`.
- **More throughput:** `CONCURRENCY` in `grader.py` sets how many sandboxes run at once.
- **Hints, not answers:** with `--feedback`, a model sees the problem, the submission and the names of failed tests, never the tests themselves, and its hint never changes a grade.

## Good to know

- Isolation comes from the sandbox and file permissions. The `blocked` outcome is a report on top: a Python audit hook flags any reach for the network or the grader's files, and such a submission scores 0 for review.
- A submission that hides its attempt from the hook still gets nothing out; it just isn't flagged.
- A call that hits the 3-second limit is killed with everything it started, and only that test fails.
- For `--feedback`, the model is `glm-4-7` by default. Set `MODEL` to try another.

## Time and cost

About 25 seconds for the six samples, plus about 25 seconds with `--feedback`. Most submissions take 4 to 8 seconds including sandbox creation; one whose network attempt hangs takes about 22. At most three sandboxes run at once, each only while its submission is graded. Every sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `grader-` sandbox from the console.
