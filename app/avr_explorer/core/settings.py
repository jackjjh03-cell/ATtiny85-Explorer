from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .paths import CONFIG_DIR


DEFAULT_SETTINGS = {
    "mode": "Basic",
    "window_geometry": "1120x690",
    "flash_dashboard_split": 540,
    "eeprom_dashboard_split": 540,
    "flash_workspace_split": 385,
    "eeprom_workspace_split": 385,
    "programmer": "usbtiny",
    "part": "t85",
    "automatic_backup": True,
    "verify_after_write": True,
    "full_readback_verification": False,
    "smart_eeprom_write": True,
    "restore_eeprom_after_flash_write": True,
    "isp_speed_mode": "Automatic",
    "last_detected_clock_mhz": 0.0,
    "operation_timeout_seconds": 45,
    "arduino_directory": "",
    "last_flash_directory": "",
    "last_eeprom_directory": "",
    "terminal_port": "",
    "terminal_baud": "9600",
    "terminal_data_bits": "8",
    "terminal_parity": "None",
    "terminal_stop_bits": "1",
    "terminal_flow_control": "None",
    "terminal_display": "Text",
    "terminal_encoding": "UTF-8",
    "terminal_line_ending": "None",
    "attiny85_device_grade": "Select chip marking",
    "attiny85_supply_voltage": "Select planned operating voltage",
}


class SettingsStore:
    def __init__(self, path: Path = CONFIG_DIR / "settings.json") -> None:
        self.path = path
        self.values: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.values.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.values, indent=2), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.save()
