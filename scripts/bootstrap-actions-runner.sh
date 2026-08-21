#!/usr/bin/env bash

# This file is embedded into cloud-init by runner_lifecycle.py. The provisioner
# prepends the required values as shell-safe, readonly variables.

set -Eeuo pipefail
umask 077

: "${RUNNER_DOWNLOAD_URL:?RUNNER_DOWNLOAD_URL is required}"
: "${RUNNER_SHA256:?RUNNER_SHA256 is required}"
: "${RUNNER_VERSION:?RUNNER_VERSION is required}"
: "${RUNNER_JIT_CONFIG:?RUNNER_JIT_CONFIG is required}"

readonly RUNNER_DIR=/opt/actions-runner
readonly RUNNER_ARCHIVE=/tmp/actions-runner.tar.gz
readonly JIT_CONFIG_FILE=/run/actions-runner-jit-config

echo "gup-bootstrap: downloading GitHub Actions Runner ${RUNNER_VERSION}"
install -d -m 0755 "${RUNNER_DIR}"
curl \
  --fail \
  --location \
  --proto '=https' \
  --retry 5 \
  --retry-all-errors \
  --silent \
  --show-error \
  --output "${RUNNER_ARCHIVE}" \
  "${RUNNER_DOWNLOAD_URL}"

printf '%s  %s\n' "${RUNNER_SHA256}" "${RUNNER_ARCHIVE}" | sha256sum --check --strict
tar --extract --gzip --file "${RUNNER_ARCHIVE}" --directory "${RUNNER_DIR}"
rm -f "${RUNNER_ARCHIVE}"

echo 'gup-bootstrap: installing runner dependencies'
export DEBIAN_FRONTEND=noninteractive
export RUNNER_ALLOW_RUNASROOT=1
"${RUNNER_DIR}/bin/installdependencies.sh"

printf '%s' "${RUNNER_JIT_CONFIG}" >"${JIT_CONFIG_FILE}"
chmod 0600 "${JIT_CONFIG_FILE}"
unset RUNNER_JIT_CONFIG

cat >/usr/local/sbin/run-actions-runner <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

readonly runner_dir=/opt/actions-runner
readonly jit_config_file=/run/actions-runner-jit-config

jit_config=$(<"${jit_config_file}")
rm -f "${jit_config_file}"

export RUNNER_ALLOW_RUNASROOT=1
cd "${runner_dir}"
exec ./run.sh --jitconfig "${jit_config}"
EOF
chmod 0700 /usr/local/sbin/run-actions-runner

cat >/etc/systemd/system/github-actions-runner.service <<'EOF'
[Unit]
Description=Ephemeral GitHub Actions JIT runner
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/actions-runner
ExecStart=/usr/local/sbin/run-actions-runner
Restart=no
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now github-actions-runner.service
echo 'gup-bootstrap: runner service started'
