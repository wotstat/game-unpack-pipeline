#!/usr/bin/env bash

# This file is embedded into cloud-init by runner_lifecycle.py. The provisioner
# prepends the required values as shell-safe variables. All JIT configurations
# are consumed once and never passed through service-unit arguments.

set -Eeuo pipefail
umask 077

: "${RUNNER_DOWNLOAD_URL:?RUNNER_DOWNLOAD_URL is required}"
: "${RUNNER_SHA256:?RUNNER_SHA256 is required}"
: "${RUNNER_VERSION:?RUNNER_VERSION is required}"
: "${BUILDER_RUNNER_JIT_CONFIG:?BUILDER_RUNNER_JIT_CONFIG is required}"
: "${WOT_GUI_ASSETS_RUNNER_JIT_CONFIG:?WOT_GUI_ASSETS_RUNNER_JIT_CONFIG is required}"
: "${WOT_SRC_RUNNER_JIT_CONFIG:?WOT_SRC_RUNNER_JIT_CONFIG is required}"

readonly RUNNER_TEMPLATE_DIR=/opt/actions-runner-template
readonly RUNNERS_ROOT=/opt/actions-runners
readonly RUNNER_ARCHIVE=/tmp/actions-runner.tar.gz

echo "gup-bootstrap: downloading GitHub Actions Runner ${RUNNER_VERSION}"
install -d -m 0755 "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}"
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
tar --extract --gzip --file "${RUNNER_ARCHIVE}" --directory "${RUNNER_TEMPLATE_DIR}"
rm -f "${RUNNER_ARCHIVE}"

echo 'gup-bootstrap: installing runner dependencies'
export DEBIAN_FRONTEND=noninteractive
"${RUNNER_TEMPLATE_DIR}/bin/installdependencies.sh"
apt-get update
apt-get install --yes --no-install-recommends git

useradd --create-home --home-dir /var/lib/snapshot-builder --shell /bin/bash snapshot-builder
useradd --create-home --home-dir /var/lib/wot-gui-assets-publisher \
  --shell /bin/bash wot-gui-assets-publisher
useradd --create-home --home-dir /var/lib/wot-src-publisher --shell /bin/bash wot-src-publisher
chmod 0700 \
  /var/lib/snapshot-builder \
  /var/lib/wot-gui-assets-publisher \
  /var/lib/wot-src-publisher
install -d -o snapshot-builder -g snapshot-builder -m 0711 /var/lib/game-snapshot-builder

install -m 0440 /dev/null /etc/sudoers.d/snapshot-builder
printf '%s\n' 'snapshot-builder ALL=(ALL) NOPASSWD: ALL' \
  >/etc/sudoers.d/snapshot-builder

cp -a "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}/builder"
cp -a "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}/wot-gui-assets"
cp -a "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}/wot-src"
chown -R snapshot-builder:snapshot-builder "${RUNNERS_ROOT}/builder"
chown -R wot-gui-assets-publisher:wot-gui-assets-publisher \
  "${RUNNERS_ROOT}/wot-gui-assets"
chown -R wot-src-publisher:wot-src-publisher "${RUNNERS_ROOT}/wot-src"
rm -rf "${RUNNER_TEMPLATE_DIR}"

install -d -o root -g root -m 0755 /run/actions-runner
install -d -o snapshot-builder -g snapshot-builder -m 0700 \
  /run/actions-runner/builder
install -d -o wot-gui-assets-publisher -g wot-gui-assets-publisher -m 0700 \
  /run/actions-runner/wot-gui-assets
install -d -o wot-src-publisher -g wot-src-publisher -m 0700 \
  /run/actions-runner/wot-src
install -o snapshot-builder -g snapshot-builder -m 0600 /dev/null \
  /run/actions-runner/builder/jit-config
install -o wot-gui-assets-publisher -g wot-gui-assets-publisher -m 0600 /dev/null \
  /run/actions-runner/wot-gui-assets/jit-config
install -o wot-src-publisher -g wot-src-publisher -m 0600 /dev/null \
  /run/actions-runner/wot-src/jit-config
printf '%s' "${BUILDER_RUNNER_JIT_CONFIG}" \
  >/run/actions-runner/builder/jit-config
printf '%s' "${WOT_GUI_ASSETS_RUNNER_JIT_CONFIG}" \
  >/run/actions-runner/wot-gui-assets/jit-config
printf '%s' "${WOT_SRC_RUNNER_JIT_CONFIG}" \
  >/run/actions-runner/wot-src/jit-config
unset \
  BUILDER_RUNNER_JIT_CONFIG \
  WOT_GUI_ASSETS_RUNNER_JIT_CONFIG \
  WOT_SRC_RUNNER_JIT_CONFIG

cat >/usr/local/sbin/run-actions-runner <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

readonly role="${1:?runner role is required}"
readonly runner_dir="/opt/actions-runners/${role}"
readonly jit_config_file="/run/actions-runner/${role}/jit-config"

jit_config=$(<"${jit_config_file}")
rm -f "${jit_config_file}"
cd "${runner_dir}"
exec ./run.sh --jitconfig "${jit_config}"
EOF
chmod 0755 /usr/local/sbin/run-actions-runner

cat >/etc/systemd/system/github-actions-runner-builder.service <<'EOF'
[Unit]
Description=Ephemeral GitHub Actions builder JIT runner
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=snapshot-builder
Group=snapshot-builder
Environment=HOME=/var/lib/snapshot-builder
WorkingDirectory=/opt/actions-runners/builder
ExecStart=/usr/local/sbin/run-actions-runner builder
Restart=no
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/github-actions-runner-wot-src.service <<'EOF'
[Unit]
Description=Ephemeral GitHub Actions wot-src JIT runner
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=wot-src-publisher
Group=wot-src-publisher
Environment=HOME=/var/lib/wot-src-publisher
WorkingDirectory=/opt/actions-runners/wot-src
ExecStart=/usr/local/sbin/run-actions-runner wot-src
Restart=no
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/github-actions-runner-wot-gui-assets.service <<'EOF'
[Unit]
Description=Ephemeral GitHub Actions wot-gui-assets JIT runner
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=wot-gui-assets-publisher
Group=wot-gui-assets-publisher
Environment=HOME=/var/lib/wot-gui-assets-publisher
WorkingDirectory=/opt/actions-runners/wot-gui-assets
ExecStart=/usr/local/sbin/run-actions-runner wot-gui-assets
Restart=no
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now \
  github-actions-runner-builder.service \
  github-actions-runner-wot-gui-assets.service \
  github-actions-runner-wot-src.service
rm -f /usr/local/sbin/bootstrap-actions-runner
echo 'gup-bootstrap: builder, wot-src and wot-gui-assets runner services started'
