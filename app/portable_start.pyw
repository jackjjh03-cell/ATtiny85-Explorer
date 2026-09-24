"""Portable launcher entry point for ATtiny85 Explorer."""

import os
import sys
import traceback
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "vendor"))
os.chdir(str(APP_ROOT))

try:
    from avr_explorer.app import run
    run()
except BaseException:
    details = traceback.format_exc()
    log_dir = Path(os.environ.get("APPDATA", str(APP_ROOT))) / "ATtiny85 Explorer" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "startup-error.txt"
        log_path.write_text(details, encoding="utf-8")
    except OSError:
        log_path = Path("the ATtiny85 Explorer log folder")

    import ctypes
    ctypes.windll.user32.MessageBoxW(
        None,
        "ATtiny85 Explorer could not start. Details were written to:\n" + str(log_path),
        "ATtiny85 Explorer",
        0x10,
    )
    os._exit(1)
