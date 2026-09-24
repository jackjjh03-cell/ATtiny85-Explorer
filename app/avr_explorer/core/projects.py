from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

PROJECT_VERSION = 1
EXPECTED_SIGNATURE = "0x1E930B"
FLASH_SIZE = 8192
EEPROM_SIZE = 512


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_project_path(path: Path) -> Path:
    if path.suffix.lower() != ".avrxproj":
        return path.with_suffix(".avrxproj")
    return path


def create_project_package(
    path: Path,
    *,
    name: str,
    notes: str,
    flash: bytes,
    eeprom: bytes,
    lfuse: int,
    hfuse: int,
    efuse: int,
    lock: int = 0xFF,
    device_grade: str = "",
    planned_vcc: str = "",
    isp_speed: str = "Automatic",
    fuse_preset_name: str = "",
    external_frequency_mhz: Optional[float] = None,
    program_flash: bool = True,
    program_eeprom: bool = True,
    apply_fuses: bool = True,
    apply_lock: bool = False,
    verify_after_write: bool = True,
    final_full_readback: bool = True,
    app_version: str = "",
) -> Path:
    if len(flash) != FLASH_SIZE:
        raise ValueError(f"Project flash image must be exactly {FLASH_SIZE} bytes")
    if len(eeprom) != EEPROM_SIZE:
        raise ValueError(f"Project EEPROM image must be exactly {EEPROM_SIZE} bytes")
    for label, value in (("LFUSE", lfuse), ("HFUSE", hfuse), ("EFUSE", efuse), ("lock", lock)):
        if not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise ValueError(f"{label} must be a byte value")

    path = normalize_project_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    manifest: Dict[str, object] = {
        "project_version": PROJECT_VERSION,
        "app_version": app_version,
        "saved_at": now,
        "name": name.strip() or path.stem,
        "notes": notes,
        "device": "ATtiny85",
        "expected_signature": EXPECTED_SIGNATURE,
        "device_grade": device_grade,
        "planned_vcc": planned_vcc,
        "isp_speed": isp_speed,
        "fuse_setup": {
            "preset_name": fuse_preset_name,
            "external_frequency_mhz": external_frequency_mhz,
        },
        "components": {
            "program_flash": bool(program_flash),
            "program_eeprom": bool(program_eeprom),
            "apply_fuses": bool(apply_fuses),
            "apply_lock": bool(apply_lock),
        },
        "verification": {
            "verify_after_write": bool(verify_after_write),
            "final_full_readback": bool(final_full_readback),
        },
        "memory": {
            "flash_size": len(flash),
            "eeprom_size": len(eeprom),
            "flash_sha256": _sha256(flash),
            "eeprom_sha256": _sha256(eeprom),
        },
        "fuses": {
            "lfuse": lfuse,
            "hfuse": hfuse,
            "efuse": efuse,
            "lock": lock,
        },
    }
    # Write atomically so a cancelled process or full disk does not replace a
    # previously valid project with a half-written ZIP. Deflate level 9 keeps
    # full 8 KB/512 B images comprehensive while making blank regions compact.
    temporary = path.with_name(path.name + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            archive.writestr("flash.bin", flash)
            archive.writestr("eeprom.bin", eeprom)
        temporary.replace(path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return path


def read_project_package(path: Path) -> Dict[str, object]:
    with zipfile.ZipFile(path, "r") as archive:
        required = {"manifest.json", "flash.bin", "eeprom.bin"}
        missing = sorted(required.difference(archive.namelist()))
        if missing:
            raise ValueError("Project is missing: " + ", ".join(missing))
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        flash = archive.read("flash.bin")
        eeprom = archive.read("eeprom.bin")

    if not isinstance(manifest, dict):
        raise ValueError("Project manifest is invalid")
    if manifest.get("project_version") != PROJECT_VERSION:
        raise ValueError(f"Unsupported project version: {manifest.get('project_version')}")
    if manifest.get("device") != "ATtiny85" or manifest.get("expected_signature") != EXPECTED_SIGNATURE:
        raise ValueError("The selected project is not an ATtiny85 Explorer project")
    if len(flash) != FLASH_SIZE or len(eeprom) != EEPROM_SIZE:
        raise ValueError("Project memory sizes are invalid")

    memory = manifest.get("memory", {})
    if not isinstance(memory, dict):
        raise ValueError("Project memory manifest is invalid")
    if memory.get("flash_sha256") != _sha256(flash):
        raise ValueError("Project Flash hash does not match; the file may be damaged")
    if memory.get("eeprom_sha256") != _sha256(eeprom):
        raise ValueError("Project EEPROM hash does not match; the file may be damaged")

    fuses = manifest.get("fuses", {})
    if not isinstance(fuses, dict):
        raise ValueError("Project fuse manifest is invalid")
    for key in ("lfuse", "hfuse", "efuse", "lock"):
        value = fuses.get(key)
        if not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise ValueError(f"Project {key} value is invalid")

    return {"manifest": manifest, "flash": flash, "eeprom": eeprom}


def project_integrity_summary(path: Path) -> Tuple[bool, str]:
    try:
        package = read_project_package(path)
        manifest = package["manifest"]
        name = manifest.get("name", path.stem) if isinstance(manifest, dict) else path.stem
        return True, f"Validated project: {name}. Flash and EEPROM sizes and SHA-256 hashes match."
    except Exception as exc:
        return False, str(exc)
