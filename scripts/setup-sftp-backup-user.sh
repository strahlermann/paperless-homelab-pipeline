#!/usr/bin/env bash
# AI-generated (Claude Code), tested on Raspberry Pi OS (Debian 13) and Manjaro.
# Create a chrooted, SFTP-only user that receives the Paperless restic backup.
# Run as root on the target machine:
#   off-site: sudo bash setup-sftp-backup-user.sh paperlessbackup /srv/backup/paperless 'ssh-ed25519 AAAA... paperless-backup-offsite'
#   NAS:      sudo bash setup-sftp-backup-user.sh backup /tank/backup/paperless 'ssh-ed25519 AAAA... paperless-backup-nas' <paperless-box-ip>
# The restic repo ends up in <chroot>/repo. Restic encrypts it, so this machine only ever sees ciphertext.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ $# -ge 3 ]] || { sed -n 2,6p "$0"; exit 2; }
USER_NAME=$1 CHROOT=$2 PUBKEY=$3 FROM=${4:-}
[[ $PUBKEY == ssh-ed25519\ * ]] || { echo "third argument must be the ssh-ed25519 public key" >&2; exit 1; }

# --- user (no shell, no password) ---
if id "$USER_NAME" >/dev/null 2>&1; then
    [[ $(getent passwd "$USER_NAME" | cut -d: -f6) == "$CHROOT" ]] \
        || { echo "user $USER_NAME already exists with another home; pick another name" >&2; exit 1; }
else
    NOLOGIN=$(command -v nologin || echo /usr/sbin/nologin)
    useradd --system --home-dir "$CHROOT" --no-create-home --shell "$NOLOGIN" --user-group "$USER_NAME"
fi
passwd -l "$USER_NAME" >/dev/null 2>&1 || true

# --- chroot: every path component must be root-owned and not group/world-writable ---
mkdir -p "$CHROOT"
chown root:root "$CHROOT"; chmod 755 "$CHROOT"
p=$CHROOT
while [[ $p != / ]]; do
    read -r owner mode < <(stat -c '%U %a' "$p")
    if [[ $owner != root ]] || (( 8#$mode & 8#022 )); then
        echo "chroot needs $p owned by root and not group/world-writable (is: $owner $mode)" >&2; exit 1
    fi
    p=$(dirname "$p")
done
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$CHROOT/repo"

# --- key, outside the chroot so the user can't change it ---
install -d -m 755 /etc/ssh/backup_authorized_keys
OPTS="restrict"; [[ -n $FROM ]] && OPTS="from=\"$FROM\",restrict"
echo "$OPTS $PUBKEY" > "/etc/ssh/backup_authorized_keys/$USER_NAME"
chmod 644 "/etc/ssh/backup_authorized_keys/$USER_NAME"

# --- sshd Match block ---
BLOCK="# paperless restic backup: SFTP only, chrooted, key only"
# other source addresses: no way in at all (the key's from= option says the same)
[[ -n $FROM ]] && BLOCK+="
Match User $USER_NAME Address *,!$FROM
    PubkeyAuthentication no
    AuthorizedKeysFile none"
BLOCK+="
Match User $USER_NAME
    AuthorizedKeysFile /etc/ssh/backup_authorized_keys/%u
    ChrootDirectory $CHROOT
    ForceCommand internal-sftp
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no"

if grep -qiE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
    TARGET=/etc/ssh/sshd_config.d/60-$USER_NAME-sftp.conf
    cp -a "$TARGET" "$TARGET.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    echo "$BLOCK" > "$TARGET"
else
    TARGET=/etc/ssh/sshd_config
    cp -a "$TARGET" "$TARGET.bak.$(date +%Y%m%d%H%M%S)"
    sed -i "/^# paperless restic backup/,\$d" "$TARGET"   # drop an earlier copy of this block (it's always last)
    printf '\n%s\n' "$BLOCK" >> "$TARGET"
fi
echo "wrote Match block to $TARGET"

# AllowUsers / AllowGroups would lock the new user out
if sshd -T 2>/dev/null | grep -qiE '^(allowusers|allowgroups) '; then
    echo "WARNING: sshd has AllowUsers/AllowGroups set; add $USER_NAME there or the login will be refused." >&2
fi

# --- validate, then reload (never reload a broken config) ---
sshd -t
echo "-- effective settings for $USER_NAME from ${FROM:-anywhere}:"
sshd -T -C "user=$USER_NAME,host=client,addr=${FROM:-203.0.113.1}" | grep -iE '^(pubkeyauthentication|chrootdirectory|forcecommand|authorizedkeysfile)'
if [[ -n $FROM ]]; then
    echo "-- from another address (must say pubkeyauthentication no):"
    sshd -T -C "user=$USER_NAME,host=other,addr=192.0.2.7" | grep -iE '^pubkeyauthentication'
fi
if systemctl list-units --type=service --all | grep -q ' sshd\.service'; then systemctl reload sshd; else systemctl reload ssh; fi
echo "OK: $USER_NAME can now use SFTP into $CHROOT (repo dir: /repo)"
