#!/usr/bin/env bash

set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

command -v aria2c >/dev/null 2>&1 || {
  echo "aria2c is required for the offline HTTP transport tests" >&2
  exit 1
}

uv sync --frozen --all-groups
npm ci --ignore-scripts --omit=dev --no-audit --no-fund
node_modules/.bin/prettier --check src/game_downloader/web_formatter.mjs
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
bash -n download.sh .github/scripts/run-stage.sh scripts/bootstrap-actions-runner.sh scripts/check.sh

ruby <<'RUBY'
require "yaml"

Dir[".github/**/*.{yml,yaml}"].sort.each do |path|
  workflow = YAML.safe_load(File.read(path), aliases: true)
  if File.dirname(path) == ".github/workflows"
    [workflow, *workflow.fetch("jobs", {}).values].each do |scope|
      concurrency = scope["concurrency"]
      next unless concurrency.is_a?(Hash) && concurrency.key?("queue")
      unless %w[single max].include?(concurrency["queue"]) &&
             (concurrency["queue"] != "max" || concurrency.fetch("cancel-in-progress", false) == false)
        abort "Invalid concurrency.queue in #{path}"
      end
    end
  end
  puts "YAML OK: #{path}"
end
RUBY

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck download.sh .github/scripts/run-stage.sh scripts/bootstrap-actions-runner.sh scripts/check.sh
else
  echo "shellcheck not installed; skipped"
fi

if command -v actionlint >/dev/null 2>&1; then
  # actionlint 1.7.12 does not recognize concurrency.queue (upstream #657).
  actionlint -ignore '^unexpected key "queue" for "concurrency" section\.'
else
  echo "actionlint not installed; skipped"
fi
