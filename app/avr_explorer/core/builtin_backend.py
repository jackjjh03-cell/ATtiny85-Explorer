from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List

from .paths import RESOURCE_ROOT


BUILTIN_BACKEND_ID = "builtin-avrdude-6.3"
BUILTIN_BACKEND_NAME = "Built-in AVRDUDE 6.3-20190619"
BUILTIN_BACKEND_ROOT = RESOURCE_ROOT / "vendor" / "avrdude-6.3"

# Hashes from the official Arduino IDE 1.8.19 Windows ZIP. Keeping these in
# application code lets the single-file release reject a partial or corrupted
# extracted backend before AVRDUDE is launched.
EXPECTED_SHA256: Dict[str, str] = {
    "bin/avrdude.exe": "b77b5409f89090f836427948980862200a88fd7900d65442e43f0c966cd82653",
    "bin/libiconv-2.dll": "cb016e794d3311c71f21d87803e10a0e1133995f62a485eb37b321cd9b9e1087",
    "bin/libusb0.dll": "00caca07869b19d10b370552ac7cc2f6f2ee246fc15db11650f6cd3f4ef9b666",
    "bin/libwinpthread-1.dll": "13dcd56189d81dc660d0488bc4d9eda5ee0cca7a62c08bd2147d8e2a308e2044",
    "etc/avrdude.conf": "43937f83cd46a8b6117feecd5766cedbd32fcf92dff840a093d5fa3c8745b736",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_builtin_backend(root: Path = BUILTIN_BACKEND_ROOT) -> List[str]:
    """Return human-readable integrity errors; an empty list means usable."""
    errors: List[str] = []
    for relative, expected in EXPECTED_SHA256.items():
        path = root / Path(relative)
        if not path.is_file():
            errors.append(f"Missing embedded backend file: {relative}")
            continue
        try:
            actual = _sha256(path)
        except OSError as exc:
            errors.append(f"Cannot read embedded backend file {relative}: {exc}")
            continue
        if actual.lower() != expected.lower():
            errors.append(f"Embedded backend hash mismatch: {relative}")
    return errors
