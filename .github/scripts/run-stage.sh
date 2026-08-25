#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly stage="${1:?pipeline stage is required}"
: "${GAME_DOWNLOADER_DATA_ROOT:?GAME_DOWNLOADER_DATA_ROOT is required}"
: "${GAME_DOWNLOADER_REPORT_DIR:?GAME_DOWNLOADER_REPORT_DIR is required}"

case "${stage}" in
  resolve) readonly sequence=010 ;;
  plan-acquisition) readonly sequence=020 ;;
  download) readonly sequence=030 ;;
  verify) readonly sequence=040 ;;
  assemble-client) readonly sequence=050 ;;
  index-vfs) readonly sequence=060 ;;
  materialize-vfs) readonly sequence=070 ;;
  plan-readable) readonly sequence=080 ;;
  transform-readable) readonly sequence=090 ;;
  decompile-actionscript) readonly sequence=100 ;;
  assemble-readable) readonly sequence=110 ;;
  generate-engine-stubs) readonly sequence=120 ;;
  finalize-readable) readonly sequence=130 ;;
  snapshot) readonly sequence=140 ;;
  *)
    echo "Unknown pipeline stage: ${stage}" >&2
    exit 2
    ;;
esac

mkdir -p "${GAME_DOWNLOADER_REPORT_DIR}"
readonly report_path="${GAME_DOWNLOADER_REPORT_DIR}/${sequence}-${stage}.json"
readonly log_path="${GAME_DOWNLOADER_REPORT_DIR}/${sequence}-${stage}.log"
readonly io_log_path="${GAME_DOWNLOADER_REPORT_DIR}/${sequence}-${stage}-iostat.log"
readonly metrics_samples_path="${GAME_DOWNLOADER_REPORT_DIR}/${sequence}-${stage}-performance.jsonl"
readonly metrics_summary_path="${GAME_DOWNLOADER_REPORT_DIR}/${sequence}-${stage}-performance.json"
readonly time_report_path="${GAME_DOWNLOADER_REPORT_DIR}/${sequence}-${stage}-time.log"
readonly metrics_script=".github/scripts/performance-metrics.py"
readonly metrics_stage="${stage}"

io_monitor_pid=""
metrics_monitor_pid=""
monitors_finished=false

finish_monitors() {
  if [[ "${monitors_finished}" == true ]]; then
    return
  fi
  monitors_finished=true

  if [[ -n "${io_monitor_pid}" ]]; then
    kill "${io_monitor_pid}" 2>/dev/null || true
    wait "${io_monitor_pid}" 2>/dev/null || true
    io_monitor_pid=""
  fi
  if [[ -n "${metrics_monitor_pid}" ]]; then
    kill "${metrics_monitor_pid}" 2>/dev/null || true
    wait "${metrics_monitor_pid}" 2>/dev/null || true
    metrics_monitor_pid=""
  fi
  if [[ -s "${metrics_samples_path}" ]]; then
    if ! python3 "${metrics_script}" summarize \
      --stage "${metrics_stage}" \
      --samples "${metrics_samples_path}" \
      --time-report "${time_report_path}" \
      --output "${metrics_summary_path}"; then
      printf 'Performance summary generation failed for %s; raw samples remain available.\n' \
        "${stage}" >&2
    fi
  fi
}

python3 "${metrics_script}" monitor \
  --stage "${metrics_stage}" \
  --data-root "${GAME_DOWNLOADER_DATA_ROOT}" \
  --samples "${metrics_samples_path}" \
  --interval-seconds "${GAME_DOWNLOADER_METRICS_INTERVAL_SECONDS:-5}" &
metrics_monitor_pid=$!
trap finish_monitors EXIT

if [[ "${stage}" == download ]]; then
  if command -v iostat >/dev/null; then
    printf 'Extended disk I/O sampled every 5 seconds.\n' >"${io_log_path}"
    env LC_ALL=C S_TIME_FORMAT=ISO \
      stdbuf -oL -eL iostat -d -x -m -t -y 5 >>"${io_log_path}" 2>&1 &
    io_monitor_pid=$!
  else
    printf 'iostat is unavailable; disk I/O was not recorded.\n' >"${io_log_path}"
  fi
fi

if [[ "${stage}" == resolve ]]; then
  if [[ -n "${GAME_DOWNLOADER_RUN_ID:-}" ]]; then
    echo "Refusing to start a second Run in the same job" >&2
    exit 2
  fi
  : "${GAME_DOWNLOADER_TARGET:?GAME_DOWNLOADER_TARGET is required}"
  : "${GAME_DOWNLOADER_CLIENT_TYPE:?GAME_DOWNLOADER_CLIENT_TYPE is required}"
  : "${GAME_DOWNLOADER_LANGUAGES:?GAME_DOWNLOADER_LANGUAGES is required}"
  command=(
    uv run --no-sync game-downloader run
    --target "${GAME_DOWNLOADER_TARGET}"
    --client-type "${GAME_DOWNLOADER_CLIENT_TYPE}"
    --languages "${GAME_DOWNLOADER_LANGUAGES}"
    --until "${stage}"
    --data-root "${GAME_DOWNLOADER_DATA_ROOT}"
    --json
  )
else
  : "${GAME_DOWNLOADER_RUN_ID:?resolve must create a Run before ${stage}}"
  command=(
    uv run --no-sync game-downloader resume
    "${GAME_DOWNLOADER_RUN_ID}"
    --until "${stage}"
    --skip-check
    --data-root "${GAME_DOWNLOADER_DATA_ROOT}"
    --json
  )
fi

echo "Executing game-downloader stage: ${stage}"
set +e
env LC_ALL=C /usr/bin/time --verbose --output="${time_report_path}" \
  "${command[@]}" >"${report_path}" 2> >(tee "${log_path}" >&2)
exit_code=$?
set -e

finish_monitors
trap - EXIT

if [[ -s "${report_path}" ]]; then
  cat "${report_path}"
  echo
  run_id=$(
    uv run --no-sync python - "${report_path}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    value = json.load(source).get("run_id", "")
if isinstance(value, str):
    print(value)
PY
  )
  if [[ -n "${run_id}" ]]; then
    if [[ -n "${GITHUB_ENV:-}" ]]; then
      printf 'GAME_DOWNLOADER_RUN_ID=%s\n' "${run_id}" >>"${GITHUB_ENV}"
    fi
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
      printf 'run_id=%s\n' "${run_id}" >>"${GITHUB_OUTPUT}"
    fi
  fi
fi

exit "${exit_code}"
