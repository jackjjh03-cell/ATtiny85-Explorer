from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def application_root() -> Path:
    """Return the read-only root containing packaged application resources."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root).resolve()
    return Path(__file__).resolve().parents[2]


def executable_root() -> Path:
    """Return the folder containing the EXE, or the source tree while developing."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return application_root()


def user_data_root() -> Path:
    """Return the persistent per-user data folder used on Windows 7 and newer."""
    override = os.environ.get("ATTINY85_EXPLORER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    roaming = os.environ.get("APPDATA", "").strip()
    if roaming:
        return Path(roaming) / "ATtiny85 Explorer"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "ATtiny85 Explorer"
    return Path.home() / ".config" / "ATtiny85 Explorer"


APP_ROOT = application_root()
RESOURCE_ROOT = APP_ROOT
DATA_DIR = user_data_root()
CONFIG_DIR = DATA_DIR / "config"
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = DATA_DIR / "backups"
TEMP_DIR = DATA_DIR / "temp"
PROFILE_DIR = DATA_DIR / "profiles"
PROJECT_DIR = DATA_DIR / "projects"


def _migrate_legacy_portable_data() -> None:
    """Copy a v0.5.x portable data folder on first launch without deleting it."""
    legacy = executable_root() / "data"
    if DATA_DIR.exists() or not legacy.is_dir() or legacy.resolve() == DATA_DIR.resolve():
        return
    try:
        shutil.copytree(str(legacy), str(DATA_DIR))
    except OSError:
        # A read-only or damaged legacy folder must not prevent the application
        # from starting with clean per-user defaults.
        pass


_migrate_legacy_portable_data()
for directory in (DATA_DIR, CONFIG_DIR, LOG_DIR, BACKUP_DIR, TEMP_DIR, PROFILE_DIR, PROJECT_DIR):
    directory.mkdir(parents=True, exist_ok=True)
