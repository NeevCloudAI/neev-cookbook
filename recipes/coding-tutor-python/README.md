# Coding tutor with a shared box

Give each student one sandbox that they and an AI tutor share across sessions: the student works in it over SSH and sees their app on a preview URL, the tutor reads their work and gives hints without rewriting it, and between sessions the box is paused with everything kept.

```text
3. Session 1: the student opens an SSH tunnel and saves their attempt
   ssh -p 64614 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@127.0.0.1 'cat > app.py'
   their count_words: return len(text.split(" "))
4. Starting their app on 0.0.0.0:8000...
   preview: https://8000-....as-south-1.neevsandbox.app
   GET /count?text=two%20%20spaces -> 200 {"text": "two  spaces", "words": 3}
5. Asking glm-4-7 to review the work (it can read and run, not write)...
   step 1: fs_read EXERCISE.md
   step 1: fs_read app.py
   step 2: exec python3 check.py
   step 3: finish
   Hints for the student:
   1. Look at the `count_words` function and compare its behavior with the test failures, especially for empty strings and multiple consecutive spaces.
   2. The exercise requires splitting on any whitespace (spaces, tabs, newlines) rather than a single character only.
   3. Python's `split()` method has two different behaviors depending on whether you pass it an argument or not - check the documentation for the difference.
   The tutor left the student's files untouched; the hints are saved in TUTOR_NOTES.md.
6. Pausing the sandbox between sessions...
   Paused in 1.2s; the preview URL now answers 503
7. Resuming for the next session...
   answering 3.5s after the resume call
8. Session 2: checking the student's work survived
   ok     files: app.py over SSH still has the student's edit
   ok     files: TUTOR_NOTES.md has the 3 hints
   ok     process: proc_... is running
   ok     preview: the same URL answers 200 {"text": "two  spaces", "words": 3}
Session state survived the pause and resume: files, server and preview URL.
   Sandbox deleted.
```

## What you need

- Python 3.11 or later
- An OpenSSH client (`ssh`) on your `PATH`
- A NeevCloud account with two API keys from **Account > API Keys**:
  - one with Resource Type **Sandboxes** (`NEEV_API_KEY`)
  - one with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)
- Your organization and project IDs (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python coding_tutor.py
```

The run needs no input and exits 0 only when the preview URL answered, the tutor gave hints without changing the student's files, and the files, the server and the preview URL all came back after the pause and resume.

To try it as the student yourself, add `--keep 15`. After the checks the script keeps the sandbox, the SSH tunnel and the preview URL open for 15 minutes and prints the `ssh` command and the URL. Open a shell, fix `count_words` in `app.py`, run `python3 check.py`, and read `TUTOR_NOTES.md`. Press `Ctrl+C` when you are done.

## How it works

The script plays the platform you would build: it owns the sandbox's lifecycle, opens SSH for the student, and connects the tutor. The student is simulated over real SSH, so the same commands work when a person types them.

1. `client.sandboxes.create({"egress": {"mode": "deny_all"}})` starts the student's sandbox with no internet access, and `sandbox.files.write` seeds the exercise from `exercise/`: a word counter served by Python's standard `http.server`, a task in `EXERCISE.md`, and `check.py` to test it.
2. `sandbox.ssh()` opens an SSH tunnel: a listener on `127.0.0.1` that forwards each connection to the sandbox over a connection authenticated with your API key, so there are no SSH keys to manage. The student saves their attempt with plain `ssh ... 'cat > app.py'`. The attempt works for "hello world" but miscounts repeated spaces and empty text.
3. `sandbox.processes.start(["python3", "app.py"])` runs their app, which binds `0.0.0.0` so the preview URL can reach it, and `sandbox.get_url(8000)` returns the URL. The script checks that `/count` answers.
4. The tutor (`tutor.py`) connects to the sandbox MCP server with the `x-sandbox-name` header, so its session is bound to this student's sandbox. It keeps three of the server's tools, `fs_read`, `fs_list` and `exec`, plus a local `finish` that takes 2 or 3 hints. It has no tool to write files, a call to any other tool is refused, and a hint containing a code block is sent back. Because `exec` can run any command, the script also reads the exercise files before and after the review and fails the run if the tutor changed one or stopped the student's server. The hints are saved in the sandbox as `TUTOR_NOTES.md` for the student's next session.
5. `sandbox.pause()` ends the session; the preview URL stops answering while the sandbox is paused. `sandbox.resume()` starts the next session, and the script waits until a command runs in the sandbox, then opens a new SSH tunnel. It reads `app.py` and `TUTOR_NOTES.md` back over SSH, checks with `sandbox.processes.get` that the student's server is the same running process, and calls the same preview URL again.

How much a hint gives away depends on the model. The prompt asks for pointers, not fixes, but in our runs `glm-4-7` often named the exact call that fixes the bug. Tighten the prompt or add a reviewing step if your course needs subtler hints.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=glm-5-2`.

## Time and cost

24 to 32 seconds end to end with `glm-4-7`, and 40 seconds with `glm-5-2`. In our runs the sandbox was paused 1.1 to 1.3 seconds after the pause call and answering 2.4 to 3.5 seconds after the resume call. The tutor gets at most 12 steps and 150 seconds, which covers its model calls and its tool calls. You pay for the sandbox for the length of the run, under a minute plus any `--keep` time, and for the model tokens the tutor uses.

## Cleanup

The sandbox is deleted when the script ends, fails or you press `Ctrl+C`, including while it is paused, and the SSH tunnel and preview URL stop working with it. In your own product you would pause the sandbox at the end of each session and delete it when the course ends. If the process is killed outright, or interrupted again while it is deleting the sandbox, delete any leftover `tutor-` sandbox from the console.
