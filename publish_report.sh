#!/usr/bin/env bash
# Linux equivalent of publish_report.ps1: copies the latest masked report to
# the GitHub Pages folder as index.html, then commits and pushes it.
#
# Usage: ./publish_report.sh [--message "Update portfolio report"] [--no-push]
set -euo pipefail

MESSAGE="Update portfolio report"
NO_PUSH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --message|-m) MESSAGE="$2"; shift 2 ;;
    --no-push) NO_PUSH=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORTS_DIR="$PROJECT_ROOT/reports"
CONFIG_PATH="$PROJECT_ROOT/config.ini"
PUBLIC_DIR="$PROJECT_ROOT/public"

if [[ -n "${MPB_PUBLIC_DIR:-}" ]]; then
  PUBLIC_DIR="$MPB_PUBLIC_DIR"
elif [[ -f "$CONFIG_PATH" ]]; then
  configured="$(awk '
    /^\[.*\]/ { in_section = ($0 ~ /^\[PUBLISH\]/); next }
    in_section && /^[[:space:]]*PUBLIC_DIR[[:space:]]*=/ {
      sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]\r]+$/, ""); print; exit
    }' "$CONFIG_PATH")"
  # A Windows path (C:\...) from a copied config.ini is ignored on Linux.
  if [[ -n "$configured" && "$configured" != *:\\* ]]; then
    PUBLIC_DIR="$configured"
  fi
fi

if [[ ! -d "$PUBLIC_DIR" ]]; then
  echo "Public repository folder not found: $PUBLIC_DIR" >&2
  exit 1
fi
command -v git >/dev/null || { echo "git was not found on PATH." >&2; exit 1; }

latest="$(ls -t "$REPORTS_DIR"/report_masked_*.html 2>/dev/null | head -n 1 || true)"
if [[ -z "$latest" ]]; then
  echo "No masked report found in $REPORTS_DIR. Generate a report first." >&2
  exit 1
fi

cp -f "$latest" "$PUBLIC_DIR/index.html"
echo "Copied latest masked report:"
echo "  $latest"
echo "to:"
echo "  $PUBLIC_DIR/index.html"

cd "$PUBLIC_DIR"
git_cmd=(git -c "safe.directory=$PUBLIC_DIR")

if [[ -z "$("${git_cmd[@]}" status --short index.html)" ]]; then
  echo "No index.html changes to publish."
  exit 0
fi

"${git_cmd[@]}" add index.html
"${git_cmd[@]}" commit -m "$MESSAGE"

if [[ "$NO_PUSH" -eq 1 ]]; then
  echo "Committed but skipped push because --no-push was provided."
else
  "${git_cmd[@]}" push -u origin main
  echo "Published report to GitHub Pages repository."
fi
