# Backups and maintenance

> 🤖 AI-generated (Claude), reviewed by the setup's owner.

## Backups

**What gets backed up:** a full `document_exporter` export (originals, archive PDFs, metadata manifest), a `pg_dump` of the database in custom format, and the compose file, env files and secrets folder. That's enough to rebuild Paperless from scratch on new hardware.

**Where:**

| Repo | Target | When |
|---|---|---|
| off-site | restic over SFTP to a small VPS, chrooted SFTP-only user | nightly at 02:30 |
| NAS | restic over SFTP to the ZFS pool on the heavy box, user locked to the driver's IP | during the 2-monthly maintenance run (the box is off otherwise) |

restic encrypts on the client, so neither target ever sees plaintext. **Without the restic password both repos are unreadable:** put it in your password manager the day you set this up.

**Retention:** `--keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune`.

**Notifications:** success and failure both go to the push topic. Silence means something is broken, so make sure someone actually reads them.

**Size:** about 250 MiB for 300 documents; the first off-site run took 6 minutes over a home uplink.

### Scripts

All in [`../scripts/`](../scripts/):

| Script | Run on | Does |
|---|---|---|
| `install-backup.sh` | the Paperless box, as root | installs restic, creates the config dir, generates the restic password and two SSH keys, writes the ssh config and the systemd units, enables the nightly timer |
| `setup-sftp-backup-user.sh` | each backup target, as root | creates a no-shell user with a chroot, key-only, optionally only from one IP; validates with `sshd -t` before reloading |
| `paperless-backup` | the Paperless box (installed by the first script) | `offsite`, `nas`, `init <repo>`, `snapshots <repo>`, `restore-test <repo>` |

Order: run `install-backup.sh`, take the two printed public keys, run `setup-sftp-backup-user.sh` on each target with its key, then `paperless-backup init offsite`, `paperless-backup offsite` and `paperless-backup restore-test offsite`.

### Restore test

`paperless-backup restore-test <repo>` restores three random original documents from the latest snapshot into `/tmp/paperless-restore-test` and compares their checksums with the live files. Do it after setup and now and then. The maintenance run also does `restic check --read-data` on the NAS repo, which reads back every stored blob.

### Gotchas

- **chroot ownership:** sshd refuses a chroot unless every path component is owned by root and not group/world-writable. Our ZFS mount point was owned by a stale numeric UID; `chown root:root` on the top two directories fixed it.
- **ssh unit name:** `sshd` on Arch/Manjaro, `ssh` on Debian/Ubuntu. The setup script checks.
- **Non-standard SSH port on the VPS:** put it in the root ssh config (`Port …`) for the backup host alias.
- **A narrow sudoers rule** lets the admin user start exactly the NAS backup unit and nothing else, so a timer on another account can trigger it without broad sudo.

## Maintenance run for the heavy box

The heavy box (local LLM, NAS) is off most of the time, so it gets one scheduled maintenance run: a systemd timer on the always-on box, **first Sunday of every second month at 04:00**.

1. Wake the box (WoL) and wait for SSH.
2. Copy the Paperless backup to the NAS repo, then `restic check --read-data`.
3. OS updates (keyrings first on Arch-based systems), trim the package cache to the last two versions. AUR packages are reported, not updated automatically.
4. Check that the ZFS kernel module is built for every installed kernel (with `zfs-dkms`, a kernel update without a matching module means the pool won't import after reboot).
5. `zpool scrub` (about 5 hours for a 14.5 TB pool, half full).
6. One push summary: updates, cache size, scrub result, number of AUR and orphaned packages.
7. Power off, unless someone is logged in, the box was already on before the job started, or the ZFS module is missing.

The always-on box reaches the heavy box with a dedicated maintenance key whose forced command only starts the maintenance script, plus a sudoers rule for exactly the commands it needs. No general-purpose sudo is left open.

The first test run also found ~28 GB of old packages in the cache (now 19 GB) and 29 AUR packages nobody used anymore (removed by hand, keeping `base-devel`).
