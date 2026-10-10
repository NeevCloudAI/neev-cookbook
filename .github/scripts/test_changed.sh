#!/usr/bin/env bash
# Runs the unit tests of every recipe and example a pull request changes: pytest for Python folders,
# npm test for TypeScript ones. Usage: test_changed.sh <base commit>. Runs in the pull request review sandbox.
set -eu
base=${1:?usage: test_changed.sh <base commit>}
failed=()

# Folders under recipes/ or examples/ that the pull request touches and that still exist at its head.
# The diff runs on its own first: a failure inside < <(...) is ignored even with set -e, and an empty list would pass.
changed=$(git diff --name-only "$base"...HEAD) || { echo "git diff against $base failed"; exit 1; }
dirs=()
while IFS= read -r dir; do
  [ -d "$dir" ] && dirs+=("$dir")
done < <(awk -F/ '($1 == "recipes" || $1 == "examples") && NF > 2 { print $1 "/" $2 }' <<< "$changed" | sort -u)
if [ ${#dirs[@]} -eq 0 ]; then
  echo "No recipe or example changed; nothing to test."
  exit 0
fi

for dir in "${dirs[@]}"; do
  echo "== $dir"
  if [ -f "$dir/requirements-dev.txt" ]; then
    # Each folder gets its own virtualenv, as the contributing guide asks.
    venv="/tmp/venv-${dir//\//-}"  # recipes/x and examples/x must not share one
    (cd "$dir" && python3 -m venv "$venv" && "$venv/bin/pip" install --disable-pip-version-check -r requirements-dev.txt \
      && "$venv/bin/pytest" -q) || failed+=("$dir")
  elif [ -f "$dir/package.json" ] && (cd "$dir" && node -e 'process.exit(require("./package.json").scripts?.test ? 0 : 1)'); then
    (cd "$dir" && npm install --no-audit --no-fund && npm test) || failed+=("$dir")
  else
    echo "No unit tests here; skipped."
  fi
done

if [ ${#failed[@]} -gt 0 ]; then
  echo "Failed: ${failed[*]}"
  exit 1
fi
echo "All changed folders passed: ${dirs[*]}"
