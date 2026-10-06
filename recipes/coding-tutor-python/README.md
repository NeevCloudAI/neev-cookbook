# Coding tutor with a shared box

Give each student one sandbox that they and an AI tutor share across sessions. The student works in it over SSH and sees their app on a preview URL; the tutor reads their work and gives hints without rewriting it; and between sessions the box is paused with everything kept.

<p align="center">
  <img src="../../assets/runs/coding-tutor-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, an OpenSSH client (`ssh`), a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python coding_tutor.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The run needs no input. It exits 0 only when the preview URL answered, the tutor gave hints without changing the student's files, and the files, server and URL all came back after the pause and resume.

To try it as the student, add `--keep 15`: after the checks, the sandbox, SSH tunnel and preview URL stay open for 15 minutes and the script prints the `ssh` command and the URL. Fix `count_words` in `app.py`, run `python3 check.py`, and read `TUTOR_NOTES.md`. Press `Ctrl+C` when you're done.

## How it works

1. **The student's box.** The script starts a sandbox with no internet access and seeds an exercise: a small word-counter web app, a task in `EXERCISE.md` and `check.py` to test it.
2. **SSH, no keys.** `sandbox.ssh()` opens a tunnel on your machine that forwards to the sandbox over a connection authenticated with your API key, so there are no SSH keys to manage. The student saves a buggy attempt over plain `ssh`.
3. **Preview.** The script starts the student's app and `sandbox.get_url(8000)` gives it a preview URL.
4. **Tutor.** An AI tutor connects over MCP with read-only tools plus `exec`, runs `check.py` and gives 2 or 3 hints, which are saved as `TUTOR_NOTES.md`. The script checks the tutor changed none of the student's files.
5. **Between sessions.** `sandbox.pause()` ends the session and the preview URL stops answering. `sandbox.resume()` starts the next one: same files, same running app, same URL.

## Use it in your product

- **One box per student:** create a sandbox per student at enrolment, pause it at the end of each session, and resume it at the next. Delete it when the course ends.
- **Your own exercises:** replace the files in `exercise/` (`EXERCISE_FILES` in `coding_tutor.py`).
- **Your own tutor style:** `SYSTEM_PROMPT` in `tutor.py` asks for pointers, not fixes. In our runs `glm-4-7` often still named the exact call that fixes the bug, so tighten the prompt or add a review step if your course needs subtler hints.
- **Browser access:** students can use the preview URL for their app, and SSH or your own web terminal for the shell.

## Good to know

- The tutor has no tool to write files, and a hint containing a code block is sent back to be rewritten. Because `exec` can run any command, the script also checks the exercise files before and after the review.
- The app binds `0.0.0.0` so the preview URL can reach it.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=glm-5-2`.

## Time and cost

24 to 32 seconds with `glm-4-7`. In our runs the sandbox paused in about 1.2 seconds and answered 2.4 to 3.5 seconds after the resume call. The tutor is limited to 12 steps and 150 seconds. You pay for the sandbox for the run (under a minute, plus any `--keep` time) and for the model tokens. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`, and the SSH tunnel and preview URL stop working with it. If the process is killed outright, delete any leftover `tutor-` sandbox from the console.
