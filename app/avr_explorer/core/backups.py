from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .intelhex import bytes_to_memory, write_intel_hex
from .models import Backend, DeviceState
from .paths import BACKUP_DIR


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_backup_package(
    flash: bytes,
    eeprom: bytes,
    state: DeviceState,
    backend: Backend,
    operation: str,
    raw_log: str = "",
    directory: Path = BACKUP_DIR,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    package_path = directory / f"ATtiny85-{timestamp}-{operation.replace(' ', '_')}.avrxpkg"

    temp_hex = directory / f".__avrx_{timestamp}.hex"
    write_intel_hex(bytes_to_memory(flash), temp_hex)
    flash_hex = temp_hex.read_bytes()
    try:
        temp_hex.unlink()
    except OSError:
        pass

    manifest: Dict[str, object] = {
        "package_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "operation": operation,
        "device": "ATtiny85",
        "expected_signature": "0x1E930B",
        "observed_signature": state.signature,
        "programmer": "usbtiny",
        "backend": {
            "name": backend.name,
            "version": backend.detected_version,
            "exe_path": backend.exe_path,
            "conf_path": backend.conf_path,
        },
        "memory": {
            "flash_size": len(flash),
            "eeprom_size": len(eeprom),
            "flash_sha256": sha256_bytes(flash),
            "eeprom_sha256": sha256_bytes(eeprom),
        },
        "fuses": {
            "lfuse": state.lfuse,
            "hfuse": state.hfuse,
            "efuse": state.efuse,
            "lock": state.lock,
        },
    }

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("flash.bin", flash)
        archive.writestr("flash.hex", flash_hex)
        archive.writestr("eeprom.bin", eeprom)
        archive.writestr("raw-avrdude.log", raw_log)

    return package_path


def read_backup_package(path: Path) -> Dict[str, object]:
    with zipfile.ZipFile(path, "r") as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        flash = archive.read("flash.bin")
        eeprom = archive.read("eeprom.bin")
        raw_log = archive.read("raw-avrdude.log").decode("utf-8", errors="replace") if "raw-avrdude.log" in archive.namelist() else ""
    return {
        "manifest": manifest,
        "flash": flash,
        "eeprom": eeprom,
        "raw_log": raw_log,
    }


def validate_backup_package(path: Path) -> Dict[str, object]:
    package = read_backup_package(path)
    manifest = package["manifest"]
    flash = package["flash"]
    eeprom = package["eeprom"]
    if not isinstance(manifest, dict):
        raise ValueError("Backup manifest is invalid")
    if manifest.get("device") != "ATtiny85" or manifest.get("expected_signature") != "0x1E930B":
        raise ValueError("The selected package is not an ATtiny85 Explorer backup")
    memory = manifest.get("memory", {})
    if not isinstance(memory, dict):
        raise ValueError("Backup memory manifest is invalid")
    expected_flash_size = int(memory.get("flash_size", -1))
    expected_eeprom_size = int(memory.get("eeprom_size", -1))
    if len(flash) != expected_flash_size or len(eeprom) != expected_eeprom_size:
        raise ValueError("Backup memory size does not match its manifest")
    if memory.get("flash_sha256") != sha256_bytes(flash):
        raise ValueError("Backup Flash hash does not match; the package may be damaged")
    if memory.get("eeprom_sha256") != sha256_bytes(eeprom):
        raise ValueError("Backup EEPROM hash does not match; the package may be damaged")
    package["valid"] = True
    return package
