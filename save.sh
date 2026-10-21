#!/usr/bin/env bash
# Save the bots' memory (state/) and dashboard data (docs/data/) to GitHub.
# The running bot owns these folders, so its copy always wins. Code changes pushed
# while it runs are kept: we commit only these two folders on top of the latest main.
# This can never get stuck on a merge conflict.
msg="${1:-Bot team: save}"
git rebase --abort >/dev/null 2>&1 || true
git merge --abort >/dev/null 2>&1 || true
for attempt in 1 2 3 4; do
  git fetch -q origin main || { sleep 5; continue; }
  git reset -q origin/main            # index = latest main; our files on disk are untouched
  git add -A state docs/data
  if git diff --cached --quiet; then echo "Nothing new to save."; exit 0; fi
  git commit -qm "$msg"
  if git push -q origin HEAD:main; then echo "Saved: $msg"; exit 0; fi
  sleep $((attempt * 5))
done
echo "Could not save after 4 tries." >&2
exit 1
