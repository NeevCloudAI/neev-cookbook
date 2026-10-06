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

**You play the student.** After the tutor's hints, the script hands the box over to you and waits. It prints an `ssh` command and your app's URL. In another terminal:

```bash
ssh -p <port> -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1
cat TUTOR_NOTES.md          # the tutor's hints
nano app.py                 # fix count_words
python3 check.py            # run the tests
```

How SSH reaches the sandbox: the script calls `sandbox.ssh()`, which opens a tunnel on your own machine, at `127.0.0.1` on a random port, and carries each connection to the sandbox over a connection authenticated with your API key. So there are no SSH keys to set up, and you copy the exact command the script prints, since the port changes every run. The tunnel lives only while the script runs, and only on the machine running it. The two `-o` options stop `ssh` from refusing or saving the tunnel's host key, which changes every time.

Open the URL to see your app answer. Press Enter in the first terminal when you're done: if you changed `app.py`, the script restarts your app so the URL shows your version, then pauses the box, resumes it, and checks your work survived.

With `--no-wait`, or with no terminal (as in CI), the run doesn't stop for you. `--keep 15` keeps the box, the SSH tunnel and the URL open for 15 minutes at the very end. The script exits 0 only when the URL answered, the tutor gave hints without changing your files, and the files, the app and the URL all came back after the pause and resume.

## How it works

1. **The student's box.** The script starts a sandbox with no internet access and seeds an exercise: a small word-counter web app, a task in `EXERCISE.md` and `check.py` to test it.
2. **SSH, no keys.** `sandbox.ssh()` opens a tunnel on your machine that forwards to the sandbox over a connection authenticated with your API key, so there are no SSH keys to manage. The student saves a buggy attempt over plain `ssh`.
3. **Preview.** The script starts the student's app and `sandbox.get_url(8000)` gives it a preview URL.
4. **Tutor.** An AI tutor connects over MCP with read-only tools plus `exec`, runs `check.py` and gives 2 or 3 hints, which are saved as `TUTOR_NOTES.md`. The script checks the tutor changed none of the student's files.
5. **Your turn.** The box is yours over SSH and on the preview URL until you press Enter. A changed `app.py` is picked up by restarting the app.
6. **Between sessions.** `sandbox.pause()` ends the session and the preview URL stops answering. `sandbox.resume()` starts the next one: same files, same running app, same URL.

## Use it in your product

- **One box per student:** create a sandbox per student at enrolment, pause it at the end of each session, and resume it at the next. Delete it when the course ends.
- **Remote students:** the tunnel here runs on the machine that runs the script. For students on their own machines, open the tunnel in your own backend or give them a terminal in the browser, and use the preview URL for their app.
- **Your own exercises:** replace the files in `exercise/` (`EXERCISE_FILES` in `coding_tutor.py`).
- **Your own tutor style:** `SYSTEM_PROMPT` in `tutor.py` asks for pointers, not fixes. In our runs `glm-4-7` often still named the exact call that fixes the bug, so tighten the prompt or add a review step if your course needs subtler hints.
- **Browser access:** students can use the preview URL for their app, and SSH or your own web terminal for the shell.

## Good to know

- The tutor has no tool to write files, and a hint containing a code block is sent back to be rewritten. Because `exec` can run any command, the script also checks the exercise files before and after the review.
- The app binds `0.0.0.0` so the preview URL can reach it.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=glm-5-2`.

## Time and cost

24 to 32 seconds with `glm-4-7`. In our runs the sandbox paused in about 1.2 seconds and answered 2.4 to 3.5 seconds after the resume call. The tutor is limited to 12 steps and 150 seconds. You pay for the sandbox for the run (under a minute, plus any `--keep` time) and for the model tokens. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`, and the SSH tunnel and preview URL stop working with it. If the process is killed outright, delete any leftover `tutor-` sandbox from the console.
