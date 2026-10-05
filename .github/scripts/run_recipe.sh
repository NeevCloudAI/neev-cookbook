#!/usr/bin/env bash
# Runs one recipe command with at most two attempts (model output varies run to run)
# and writes a result record for the summary job.
# Usage: run_recipe.sh <name> <dir> <command...>
set -u
name="$1"; dir="$2"; shift 2
attempts=0; status=fail; start=$(date +%s)
for attempt in 1 2; do
  attempts=$attempt
  echo "::group::$name, attempt $attempt"
  (cd "$dir" && "$@"); code=$?
  echo "::endgroup::"
  echo "$name attempt $attempt exited $code"
  if [ "$code" -eq 0 ]; then status=pass; break; fi
done
mkdir -p results
printf '{"name": "%s", "status": "%s", "duration_s": %d, "model": "%s", "attempts": %d}\n' \
  "$name" "$status" "$(( $(date +%s) - start ))" "${MODEL:-recipe default}" "$attempts" > "results/$name.json"
[ "$status" = pass ]
