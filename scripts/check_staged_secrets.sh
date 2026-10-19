#!/usr/bin/env bash
# Fails if any staged file looks like it contains an API key or is a .env file.
# Run before every commit: `make check-secrets`.
set -euo pipefail

status=0
staged=$(git diff --cached --name-only --diff-filter=ACM)

for f in $staged; do
  case "$f" in
    .env|*/.env|.env.*) [[ "$f" == *.env.example ]] || { echo "refusing to commit env file: $f"; status=1; } ;;
  esac
done

pattern='(gsk_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'
if git diff --cached -U0 | grep -E '^\+' | grep -Eq "$pattern"; then
  echo "staged changes contain something that looks like an API key:"
  git diff --cached -U0 | grep -E '^\+' | grep -Eo "$pattern" | sed -E 's/(.{8}).*/\1…/' | sort -u
  status=1
fi

[[ $status -eq 0 ]] && echo "secret check: clean"
exit $status
