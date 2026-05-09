#!/usr/bin/env python3
"""
backup.py — Back up nba.db to Google Drive using rclone.

Usage:
    python backup.py              # backup now
    python backup.py --check      # verify rclone is configured correctly

Setup (one-time):
    1. Run: rclone config
    2. Choose "n" for new remote, name it "gdrive"
    3. Choose Google Drive as the storage type
    4. Follow the browser auth flow
    5. Run: python backup.py --check
"""

import subprocess
import sys
import os
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────
RCLONE_REMOTE = "gdrive"          # Name you give the remote in `rclone config`
REMOTE_FOLDER = "nba_backup"      # Folder on Google Drive to store backups
DB_FILE       = "nba.db"          # Local database file
KEEP_BACKUPS  = 10                # How many timestamped backups to keep on Drive
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, DB_FILE)


def check_rclone():
    """Verify rclone is installed and the remote is configured."""
    try:
        subprocess.run(["rclone", "version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("❌  rclone is not installed. Run: brew install rclone")
        sys.exit(1)

    result = subprocess.run(
        ["rclone", "listremotes"], capture_output=True, text=True, check=True
    )
    remotes = result.stdout.strip().splitlines()
    configured = [r.rstrip(":") for r in remotes]

    if RCLONE_REMOTE not in configured:
        print(f"❌  Remote '{RCLONE_REMOTE}' not found in rclone config.")
        print(f"    Configured remotes: {configured or 'none'}")
        print(f"    Run: rclone config   (name your Google Drive remote '{RCLONE_REMOTE}')")
        sys.exit(1)

    print(f"✅  rclone is set up and remote '{RCLONE_REMOTE}' is configured.")

    # Quick connectivity test
    test = subprocess.run(
        ["rclone", "lsd", f"{RCLONE_REMOTE}:"],
        capture_output=True, text=True
    )
    if test.returncode != 0:
        print("⚠️   Could not list Drive root — check auth or internet connection.")
        print(test.stderr.strip())
    else:
        print(f"✅  Successfully connected to Google Drive.")


def backup():
    """Copy nba.db to Google Drive with a timestamped filename."""
    check_rclone()

    if not os.path.exists(DB_PATH):
        print(f"❌  Database not found at {DB_PATH}")
        sys.exit(1)

    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_name = f"nba_{timestamp}.db"
    destination = f"{RCLONE_REMOTE}:{REMOTE_FOLDER}/{remote_name}"

    print(f"📦  Backing up {DB_FILE} → {destination} ...")
    result = subprocess.run(
        ["rclone", "copyto", DB_PATH, destination, "--progress"],
        text=True
    )

    if result.returncode != 0:
        print("❌  Backup failed.")
        sys.exit(1)

    print(f"✅  Backup complete: {remote_name}")
    prune_old_backups()


def prune_old_backups():
    """Delete oldest backups if more than KEEP_BACKUPS exist."""
    result = subprocess.run(
        ["rclone", "lsf", f"{RCLONE_REMOTE}:{REMOTE_FOLDER}/",
         "--files-only", "--format", "tp"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return  # Non-fatal — skip pruning

    lines = [l for l in result.stdout.strip().splitlines() if l.endswith(".db")]
    # lsf --format tp gives "YYYY-MM-DD HH:MM:SS;filename" — sort by timestamp
    lines.sort()

    if len(lines) > KEEP_BACKUPS:
        to_delete = lines[:len(lines) - KEEP_BACKUPS]
        for entry in to_delete:
            filename = entry.split(";")[-1].strip()
            subprocess.run(
                ["rclone", "deletefile",
                 f"{RCLONE_REMOTE}:{REMOTE_FOLDER}/{filename}"],
                capture_output=True
            )
            print(f"🗑️   Pruned old backup: {filename}")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check_rclone()
    else:
        backup()