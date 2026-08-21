#!/usr/bin/env bash

set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

python_cache_dir=$(mktemp -d)
trap 'rm -rf -- "${python_cache_dir}"' EXIT

PYTHONPYCACHEPREFIX="${python_cache_dir}" python3 -m compileall -q scripts tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
bash -n scripts/bootstrap-actions-runner.sh scripts/check.sh

ruby <<'RUBY'
require "yaml"

Dir[".github/**/*.{yml,yaml}"].sort.each do |path|
  YAML.safe_load(File.read(path), aliases: true)
  puts "YAML OK: #{path}"
end
RUBY

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck scripts/bootstrap-actions-runner.sh scripts/check.sh
else
  echo "shellcheck not installed; skipped"
fi

if command -v actionlint >/dev/null 2>&1; then
  actionlint
else
  echo "actionlint not installed; skipped"
fi
