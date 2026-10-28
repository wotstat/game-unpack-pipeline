#!/usr/bin/env bash

# This file is uploaded over one-time SSH by runner_lifecycle.py. The provisioner
# prepends the required values as shell-safe variables. All JIT configurations
# are consumed once and never passed through service-unit arguments.

set -Eeuo pipefail
umask 077

: "${RUNNER_DOWNLOAD_URL:?RUNNER_DOWNLOAD_URL is required}"
: "${RUNNER_SHA256:?RUNNER_SHA256 is required}"
: "${RUNNER_VERSION:?RUNNER_VERSION is required}"
: "${DOWNLOADER_RUNNER_JIT_CONFIG:?DOWNLOADER_RUNNER_JIT_CONFIG is required}"
: "${WOT_GUI_ASSETS_RUNNER_JIT_CONFIG:?WOT_GUI_ASSETS_RUNNER_JIT_CONFIG is required}"
: "${WOT_SRC_RUNNER_JIT_CONFIG:?WOT_SRC_RUNNER_JIT_CONFIG is required}"
: "${WOTSTAT_ASSETS_RUNNER_JIT_CONFIG:?WOTSTAT_ASSETS_RUNNER_JIT_CONFIG is required}"

readonly RUNNER_TEMPLATE_DIR=/opt/actions-runner-template
readonly RUNNERS_ROOT=/opt/actions-runners
readonly RUNNER_ARCHIVE=/tmp/actions-runner.tar.gz

echo 'gup-bootstrap: arming four-hour emergency self-destruct timer'
systemctl daemon-reload
systemctl enable --now gup-emergency-self-destruct.timer

# Establish egress-only networking before any runner or job receives credentials.
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends nftables python3 curl sudo ca-certificates
cat >/etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset
table inet gup {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    # IPv6 neighbour discovery and path MTU are required for working egress.
    meta l4proto ipv6-icmp icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, packet-too-big, destination-unreachable, time-exceeded } accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output { type filter hook output priority 0; policy accept; }
}
EOF
nft --check --file /etc/nftables.conf
systemctl enable --now nftables
# Revoke the bootstrap key and password login; runtime jobs need no SSH ingress.
rm -f /root/.ssh/authorized_keys
install -d -m 0755 /etc/ssh/sshd_config.d
printf '%s\n' 'PasswordAuthentication no' 'PermitRootLogin no' >/etc/ssh/sshd_config.d/00-gup.conf
systemctl disable ssh.socket ssh.service

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

useradd --create-home --home-dir /var/lib/game-downloader --shell /bin/bash game-downloader
useradd --create-home --home-dir /var/lib/wot-gui-assets-publisher \
  --shell /bin/bash wot-gui-assets-publisher
useradd --create-home --home-dir /var/lib/wot-src-publisher --shell /bin/bash wot-src-publisher
useradd --create-home --home-dir /var/lib/wotstat-assets-uploader \
  --shell /bin/bash wotstat-assets-uploader
chmod 0700 \
  /var/lib/game-downloader \
  /var/lib/wot-gui-assets-publisher \
  /var/lib/wot-src-publisher \
  /var/lib/wotstat-assets-uploader
install -d -o game-downloader -g game-downloader -m 0711 /var/lib/game-downloader-data

install -m 0440 /dev/null /etc/sudoers.d/game-downloader
printf '%s\n' 'game-downloader ALL=(ALL) NOPASSWD: ALL' \
  >/etc/sudoers.d/game-downloader

cp -a "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}/downloader"
cp -a "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}/wot-gui-assets"
cp -a "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}/wot-src"
cp -a "${RUNNER_TEMPLATE_DIR}" "${RUNNERS_ROOT}/wotstat-assets"
chown -R game-downloader:game-downloader "${RUNNERS_ROOT}/downloader"
chown -R wot-gui-assets-publisher:wot-gui-assets-publisher \
  "${RUNNERS_ROOT}/wot-gui-assets"
chown -R wot-src-publisher:wot-src-publisher "${RUNNERS_ROOT}/wot-src"
chown -R wotstat-assets-uploader:wotstat-assets-uploader \
  "${RUNNERS_ROOT}/wotstat-assets"
rm -rf "${RUNNER_TEMPLATE_DIR}"

install -d -o root -g root -m 0755 /run/actions-runner
install -d -o game-downloader -g game-downloader -m 0700 \
  /run/actions-runner/downloader
install -d -o wot-gui-assets-publisher -g wot-gui-assets-publisher -m 0700 \
  /run/actions-runner/wot-gui-assets
install -d -o wot-src-publisher -g wot-src-publisher -m 0700 \
  /run/actions-runner/wot-src
install -d -o wotstat-assets-uploader -g wotstat-assets-uploader -m 0700 \
  /run/actions-runner/wotstat-assets
install -o game-downloader -g game-downloader -m 0600 /dev/null \
  /run/actions-runner/downloader/jit-config
install -o wot-gui-assets-publisher -g wot-gui-assets-publisher -m 0600 /dev/null \
  /run/actions-runner/wot-gui-assets/jit-config
install -o wot-src-publisher -g wot-src-publisher -m 0600 /dev/null \
  /run/actions-runner/wot-src/jit-config
install -o wotstat-assets-uploader -g wotstat-assets-uploader -m 0600 /dev/null \
  /run/actions-runner/wotstat-assets/jit-config
printf '%s' "${DOWNLOADER_RUNNER_JIT_CONFIG}" \
  >/run/actions-runner/downloader/jit-config
printf '%s' "${WOT_GUI_ASSETS_RUNNER_JIT_CONFIG}" \
  >/run/actions-runner/wot-gui-assets/jit-config
printf '%s' "${WOT_SRC_RUNNER_JIT_CONFIG}" \
  >/run/actions-runner/wot-src/jit-config
printf '%s' "${WOTSTAT_ASSETS_RUNNER_JIT_CONFIG}" \
  >/run/actions-runner/wotstat-assets/jit-config
unset \
  DOWNLOADER_RUNNER_JIT_CONFIG \
  WOT_GUI_ASSETS_RUNNER_JIT_CONFIG \
  WOT_SRC_RUNNER_JIT_CONFIG \
  WOTSTAT_ASSETS_RUNNER_JIT_CONFIG

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

cat >/etc/systemd/system/github-actions-runner-downloader.service <<'EOF'
[Unit]
Description=Ephemeral GitHub Actions downloader JIT runner
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=game-downloader
Group=game-downloader
Environment=HOME=/var/lib/game-downloader
WorkingDirectory=/opt/actions-runners/downloader
ExecStart=/usr/local/sbin/run-actions-runner downloader
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

cat >/etc/systemd/system/github-actions-runner-wotstat-assets.service <<'EOF'
[Unit]
Description=Ephemeral GitHub Actions wotstat-assets JIT runner
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=wotstat-assets-uploader
Group=wotstat-assets-uploader
Environment=HOME=/var/lib/wotstat-assets-uploader
WorkingDirectory=/opt/actions-runners/wotstat-assets
ExecStart=/usr/local/sbin/run-actions-runner wotstat-assets
Restart=no
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now \
  github-actions-runner-downloader.service \
  github-actions-runner-wot-gui-assets.service \
  github-actions-runner-wot-src.service \
  github-actions-runner-wotstat-assets.service
rm -f /usr/local/sbin/bootstrap-actions-runner
systemd-run --unit=gup-close-ssh --on-active=15s /bin/systemctl stop ssh.socket ssh.service
touch /run/gup-bootstrap.ready
echo 'gup-bootstrap: downloader, wot-src, wot-gui-assets and wotstat-assets runner services started'
