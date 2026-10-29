#!/usr/bin/env bash

set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

command -v aria2c >/dev/null 2>&1 || {
  echo "aria2c is required for the offline HTTP transport tests" >&2
  exit 1
}

uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
bash -n download.sh .github/scripts/run-stage.sh scripts/bootstrap-actions-runner.sh scripts/check.sh

ruby <<'RUBY'
require "yaml"

Dir[".github/**/*.{yml,yaml}"].sort.each do |path|
  YAML.safe_load(File.read(path), aliases: true)
  puts "YAML OK: #{path}"
end
RUBY

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck download.sh .github/scripts/run-stage.sh scripts/bootstrap-actions-runner.sh scripts/check.sh
else
  echo "shellcheck not installed; skipped"
fi

if command -v actionlint >/dev/null 2>&1; then
  actionlint
else
  echo "actionlint not installed; skipped"
fi
