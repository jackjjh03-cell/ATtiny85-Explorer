from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any


@dataclass
class Backend:
    backend_id: str
    name: str
    exe_path: str
    conf_path: str
    known_good: bool = False
    preferred: bool = False
    fallback: bool = False
    enabled: bool = True
    detected_version: str = ""
    last_test_status: str = "Not tested"
    last_tested_at: str = ""
    built_in: bool = False

    @property
    def exe(self) -> Path:
        return Path(self.exe_path)

    @property
    def conf(self) -> Path:
        return Path(self.conf_path)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Backend":
        return cls(**value)


@dataclass
class RunResult:
    command: List[str]
    exit_code: int
    output: str
    duration_seconds: float
    timed_out: bool = False
    signature: str = ""
    version: str = ""
    completed_marker: bool = False
    verification_error: bool = False
    initialization_failed: bool = False
    classification: str = "unknown"

    @property
    def ok(self) -> bool:
        return self.classification in ("success", "soft_success")


@dataclass
class DeviceState:
    signature: str = ""
    lfuse: Optional[int] = None
    hfuse: Optional[int] = None
    efuse: Optional[int] = None
    lock: Optional[int] = None
    calibration: bytes = b""
    backend_name: str = ""
    raw_output: str = ""

    @property
    def is_attiny85(self) -> bool:
        return self.signature.lower().replace("0x", "") == "1e930b"

    def fuse_dict(self) -> Dict[str, Optional[int]]:
        return {
            "lfuse": self.lfuse,
            "hfuse": self.hfuse,
            "efuse": self.efuse,
            "lock": self.lock,
        }


@dataclass
class ImageInfo:
    file_type: str
    path: str
    minimum_address: int
    maximum_address: int
    occupied_bytes: int
    ranges: List[List[int]] = field(default_factory=list)
    checksum_valid: bool = True
    sha256: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class RiskItem:
    level: str
    title: str
    detail: str
    phrase: str = ""


@dataclass
class OperationOutcome:
    success: bool
    title: str
    detail: str
    run_results: List[RunResult] = field(default_factory=list)
    backup_path: str = ""
    mismatch_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)
