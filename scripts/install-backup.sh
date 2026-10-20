#!/usr/bin/env bash
# AI-generated (Claude Code), tested on Raspberry Pi OS (Debian 13) and Manjaro.
# Install the Paperless restic backup on the box that runs Paperless. Run once as root:
#   sudo OFFSITE_HOST=backup.example.net OFFSITE_PORT=22 NAS_HOST=192.0.2.10 \
#        NTFY_URL=https://ntfy.sh/<your-secret-topic> ADMIN_USER=$USER bash install-backup.sh
# Safe to re-run: existing keys, password and config are kept.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)
CONF=/etc/paperless-backup

: "${OFFSITE_HOST:?set OFFSITE_HOST}" "${NAS_HOST:?set NAS_HOST}" "${NTFY_URL:?set NTFY_URL}" "${ADMIN_USER:?set ADMIN_USER}"
OFFSITE_PORT=${OFFSITE_PORT:-22}
OFFSITE_USER=${OFFSITE_USER:-paperlessbackup}
NAS_USER=${NAS_USER:-backup}

echo "== restic"
command -v restic >/dev/null || apt-get install -y restic

echo "== config dir, password, SSH keys"
install -d -m 700 "$CONF" /var/cache/restic /srv/paperless-backup-staging
if [[ ! -s $CONF/restic-password ]]; then
    ( umask 077; head -c 48 /dev/urandom | base64 -w0 > "$CONF/restic-password" )
    NEWPW=1
fi
for k in offsite nas; do
    [[ -f $CONF/${k}_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C "paperless-backup-$k" -f "$CONF/${k}_ed25519"
done
if [[ ! -f $CONF/config ]]; then
    cat > "$CONF/config" <<CFG
# Paperless backup config (sourced by /usr/local/sbin/paperless-backup)
NTFY_URL=$NTFY_URL
OFFSITE_REPO=sftp:paperless-offsite:/repo
NAS_REPO=sftp:paperless-nas:/repo
RESTORE_OWNER=$ADMIN_USER
CFG
fi
chmod 600 "$CONF"/*; chmod 644 "$CONF"/*.pub

echo "== root ssh config"
install -d -m 700 /root/.ssh
if ! grep -q '^Host paperless-offsite' /root/.ssh/config 2>/dev/null; then
    cat >> /root/.ssh/config <<SSH

# --- paperless-backup (restic over SFTP) ---
Host paperless-offsite
    HostName $OFFSITE_HOST
    Port $OFFSITE_PORT
    User $OFFSITE_USER
    IdentityFile $CONF/offsite_ed25519
Host paperless-nas
    HostName $NAS_HOST
    User $NAS_USER
    IdentityFile $CONF/nas_ed25519
Host paperless-offsite paperless-nas
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking accept-new
    ServerAliveInterval 30
    ServerAliveCountMax 6
SSH
    chmod 600 /root/.ssh/config
fi

echo "== script and systemd units"
install -m 755 "$SRC/paperless-backup" /usr/local/sbin/paperless-backup
cat > /etc/systemd/system/paperless-backup.service <<'U'
[Unit]
Description=Paperless backup: export, pg_dump, restic to off-site
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/paperless-backup offsite
TimeoutStartSec=3h
Nice=10
IOSchedulingClass=idle
U
cat > /etc/systemd/system/paperless-backup.timer <<'U'
[Unit]
Description=Nightly Paperless backup at 02:30

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true

[Install]
WantedBy=timers.target
U
cat > /etc/systemd/system/paperless-backup-nas.service <<'U'
[Unit]
Description=Paperless backup: restic copy to the NAS (started while the NAS box is awake)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/paperless-backup nas
TimeoutStartSec=1h
Nice=10
U
systemd-analyze verify /etc/systemd/system/paperless-backup.service /etc/systemd/system/paperless-backup.timer \
    /etc/systemd/system/paperless-backup-nas.service
systemctl daemon-reload
systemctl enable --now paperless-backup.timer

echo "== sudoers: $ADMIN_USER may start only the NAS backup unit"
TMP=$(mktemp)
echo "$ADMIN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start paperless-backup-nas.service" > "$TMP"
visudo -cf "$TMP"
install -m 440 -o root -g root "$TMP" /etc/sudoers.d/paperless-backup-nas
rm -f "$TMP"
visudo -c

echo
echo "== Done. Public keys for setup-sftp-backup-user.sh on each target:"
echo "off-site: $(cat $CONF/offsite_ed25519.pub)"
echo "NAS:      $(cat $CONF/nas_ed25519.pub)"
if [[ ${NEWPW:-0} == 1 ]]; then
    echo
    echo "!! A new restic password was created in $CONF/restic-password."
    echo "!! Without it the backups cannot be decrypted. Save it in your password manager now:"
    echo "!!     sudo cat $CONF/restic-password"
fi
