from __future__ import annotations

import hashlib
import shlex
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .avrdude import AvrDudeRunner
from .backends import BackendRegistry
from .backups import create_backup_package, read_backup_package
from .fuses import FuseConfig
from .intelhex import (
    bytes_to_memory,
    compare_payload,
    load_image,
    memory_to_bytes,
    parse_intel_hex,
    write_intel_hex,
)
from .models import Backend, DeviceState, OperationOutcome, RunResult
from .memory_tools import analyze_memory
from .paths import APP_ROOT
from .settings import SettingsStore


FLASH_SIZE = 8192
EEPROM_SIZE = 512
FLASH_PAGE_SIZE = 64
EEPROM_PAGE_SIZE = 4
EXPECTED_SIGNATURE = "0x1E930B"
SELFTEST_REPORT_BASE = 0x01E0
SELFTEST_REPORT_SIZE = 32
SELFTEST_HEX_SHA256 = "d1aee424589b2d730eeec64805b62dd9a37483409d0c86ce41d768dbe4e01411"


class OperationService:
    def __init__(
        self,
        registry: BackendRegistry,
        settings: SettingsStore,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.runner = AvrDudeRunner(
            programmer=str(settings.get("programmer", "usbtiny")),
            part=str(settings.get("part", "t85")),
        )
        self._apply_isp_speed()

    def _log(self, text: str) -> None:
        if self.log_callback:
            self.log_callback(text)

    def _progress(self, percent: int, stage: str, detail: str = "") -> None:
        callback = getattr(self, "progress_callback", None)
        if callback:
            callback(max(0, min(100, int(percent))), stage, detail)

    def _effective_bitclock_us(self, frequency_mhz: Optional[float] = None) -> float:
        mode = str(self.settings.get("isp_speed_mode", "Automatic"))
        if mode.startswith("Fast"):
            return 1.0
        if mode.startswith("Slow"):
            return 40.0
        if mode.startswith("Compatible"):
            return 10.0
        if frequency_mhz is None:
            try:
                frequency_mhz = float(self.settings.get("last_detected_clock_mhz", 0.0))
            except (TypeError, ValueError):
                frequency_mhz = 0.0
        # Keep a margin below the target's maximum ISP clock. High-speed
        # targets still get a large speedup, while factory 1 MHz parts use a
        # conservative period that remains reliable with USBtiny/FabISP wiring.
        if frequency_mhz >= 8.0:
            return 1.0
        if frequency_mhz >= 4.0:
            return 2.0
        if frequency_mhz >= 1.0:
            return 10.0
        if frequency_mhz >= 0.4:
            return 20.0
        if frequency_mhz > 0.0:
            return 40.0
        return 10.0

    def _apply_isp_speed(self, frequency_mhz: Optional[float] = None) -> float:
        period = self._effective_bitclock_us(frequency_mhz)
        self.runner.bitclock_us = period
        return period

    def _remember_detected_clock(self, state: DeviceState) -> Optional[float]:
        if state.lfuse is None or state.hfuse is None or state.efuse is None:
            return None
        try:
            frequency = FuseConfig.decode(state.lfuse, state.hfuse, state.efuse).nominal_frequency_mhz()
        except Exception:
            frequency = None
        if frequency is not None:
            self.settings.values["last_detected_clock_mhz"] = float(frequency)
            self.settings.save()
            self._apply_isp_speed(float(frequency))
        return frequency

    def _timeout(self) -> int:
        return int(self.settings.get("operation_timeout_seconds", 45))

    def _memory_timeout(self, memory: str, action: str) -> int:
        """Return a conservative overall timeout for legacy AVRDUDE/USBtiny transfers.

        The user setting remains the floor. Full EEPROM writes can take much
        longer than 45 seconds with older Windows 7 USB stacks, especially when
        AVRDUDE also verifies the write. The allowance scales with the selected
        ISP bit-clock period so choosing a slower speed does not make the app kill
        an otherwise healthy operation near the end.
        """
        base = max(15, self._timeout())
        try:
            period = float(self.runner.bitclock_us or 10.0)
        except (TypeError, ValueError):
            period = 10.0
        slow_extra = max(0.0, period - 1.0)
        if memory == "eeprom" and action == "write":
            return max(base, int(180 + slow_extra * 4))
        if memory == "flash" and action == "write":
            return max(base, int(240 + slow_extra * 3))
        if action == "read":
            minimum = 150 if memory == "flash" else 90
            return max(base, int(minimum + slow_extra * 2))
        return base

    def _transfer_progress(self, start: int, end: int, stage: str):
        span = max(1, end - start)

        def callback(action: str, percent: int) -> None:
            mapped = start + int(span * max(0, min(100, percent)) / 100)
            self._progress(mapped, f"{stage} — {action}", f"AVRDUDE {action.lower()} {percent}%")

        return callback

    def _preferred(self) -> Backend:
        backend = self.registry.preferred()
        if not backend:
            raise RuntimeError("No AVRDUDE backend is configured. Add the Arduino 1.8.19 AVRDUDE 6.3 backend first.")
        return backend

    def _fallback(self) -> Backend:
        return self.registry.fallback() or self._preferred()

    def detect(
        self,
        backend: Optional[Backend] = None,
        report_progress: bool = True,
    ) -> Tuple[DeviceState, List[RunResult]]:
        selected = backend or self._preferred()
        if report_progress:
            self._progress(8, "Connecting to programmer", "Reading the target signature and configuration bytes")
        initial_period = self._apply_isp_speed()
        state, result = self.runner.read_state(selected, timeout=self._timeout(), log_callback=self._log)
        results = [result]

        # A fast ISP setting can fail after swapping in a slower-clocked chip.
        # Retry read-only detection at progressively safer periods before giving up.
        if not result.ok or not state.is_attiny85:
            for retry_period in (10.0, 40.0):
                if retry_period <= initial_period:
                    continue
                self._log(f"Detection failed at -B {initial_period:g}; retrying safely at -B {retry_period:g}.")
                self.runner.bitclock_us = retry_period
                state, retry_result = self.runner.read_state(selected, timeout=self._timeout(), log_callback=self._log)
                results.append(retry_result)
                if retry_result.ok and state.is_attiny85:
                    break

        if (not results[-1].ok or not state.is_attiny85) and selected.backend_id != self._fallback().backend_id:
            fallback = self._fallback()
            self._log(f"Read-only detection failed. Retrying with fallback backend: {fallback.name}")
            state, fallback_result = self.runner.read_state(fallback, timeout=self._timeout(), log_callback=self._log)
            results.append(fallback_result)

        frequency = self._remember_detected_clock(state)
        if frequency is None:
            self._apply_isp_speed()
        if report_progress:
            self._progress(100, "Detection complete" if state.is_attiny85 else "Detection failed", state.signature or "No signature")
        return state, results

    def _detect_quiet(self, backend: Optional[Backend] = None) -> Tuple[DeviceState, List[RunResult]]:
        """Internal detection that does not overwrite the surrounding operation's progress stage."""
        try:
            return self.detect(backend=backend, report_progress=False)
        except TypeError as exc:
            # Keep older test doubles and third-party wrappers that only accept the
            # original single backend argument working.
            if "report_progress" not in str(exc):
                raise
            return self.detect(backend)

    def connection_stability_test(self, samples: int = 8) -> OperationOutcome:
        """Read identity/configuration repeatedly without changing the target."""
        samples = max(3, min(20, int(samples)))
        backend = self._preferred()
        period = self._apply_isp_speed()
        results: List[RunResult] = []
        readings: List[Tuple[str, Optional[int], Optional[int], Optional[int], Optional[int]]] = []
        failures: List[str] = []
        for index in range(samples):
            percent = 5 + int((index / max(1, samples)) * 88)
            self._progress(
                percent,
                "Testing connection stability",
                f"State read {index + 1} of {samples} reads at -B {period:g} µs",
            )
            state, result = self.runner.read_state(
                backend, timeout=max(30, self._timeout()), log_callback=self._log
            )
            results.append(result)
            reading = (state.signature, state.lfuse, state.hfuse, state.efuse, state.lock)
            readings.append(reading)
            if not result.ok or not state.is_attiny85:
                failures.append(
                    f"Read {index + 1}: {result.classification}; signature={state.signature or 'none'}"
                )
        stable = not failures and len(set(readings)) == 1
        if stable:
            signature, lfuse, hfuse, efuse, lock = readings[0]
            detail = (
                f"Passed {samples} identical state reads at -B {period:g} µs. "
                f"Signature {signature}; LFUSE=0x{lfuse:02X}, HFUSE=0x{hfuse:02X}, "
                f"EFUSE=0x{efuse:02X}, lock=0x{lock:02X}."
            )
            self._progress(100, "Connection is stable", f"{samples} identical state reads")
            return OperationOutcome(
                True, "Connection stability test passed", detail, results,
                extra={"samples": samples, "readings": readings, "bitclock_us": period},
            )

        unique = len(set(readings))
        detail_lines = [
            f"The programmer completed {samples} repeated state reads at -B {period:g} µs.",
            f"Unique read results: {unique} result{'s' if unique != 1 else ''}.",
        ]
        if failures:
            detail_lines.append("Failed reads:\n" + "\n".join(failures))
        else:
            detail_lines.append(
                "The signature or configuration bytes changed between reads. Check the socket, "
                "USB connection, target power, ground, and ISP wiring before writing."
            )
        self._progress(100, "Connection test failed", "Readings were not identical")
        return OperationOutcome(
            False, "Connection stability test failed", "\n\n".join(detail_lines), results,
            extra={"samples": samples, "readings": readings, "bitclock_us": period},
        )

    def test_backend(self, backend: Backend) -> Tuple[DeviceState, RunResult]:
        state, result = self.runner.read_state(backend, timeout=self._timeout(), log_callback=self._log)
        status = "Compatible" if result.ok and state.is_attiny85 else f"Failed: {result.classification}"
        self.registry.update_test(backend.backend_id, status, result.version)
        return state, result

    def _read_memory_with_fallback(
        self,
        memory: str,
        size: int,
        preferred: Optional[Backend] = None,
        progress_callback=None,
    ) -> Tuple[bytes, List[RunResult], Backend]:
        selected = preferred or self._preferred()
        timeout = self._memory_timeout(memory, "read")
        data, result = self.runner.read_memory(
            selected, memory, size, timeout=timeout, log_callback=self._log,
            progress_callback=progress_callback,
        )
        results = [result]
        used_backend = selected
        if not result.ok and selected.backend_id != self._fallback().backend_id:
            fallback = self._fallback()
            self._log(f"Read failed. Retrying {memory} with fallback backend: {fallback.name}")
            data, fallback_result = self.runner.read_memory(
                fallback, memory, size, timeout=timeout, log_callback=self._log,
                progress_callback=progress_callback,
            )
            results.append(fallback_result)
            used_backend = fallback
        return data, results, used_backend

    def _full_backup(
        self,
        operation: str,
        progress_start: int = 5,
        progress_end: int = 75,
    ) -> Tuple[Path, DeviceState, bytes, bytes, List[RunResult]]:
        span = max(1, progress_end - progress_start)
        self._progress(progress_start, "Checking target", "Confirming the connected device before backup")
        state, state_results = self._detect_quiet()
        if not state.is_attiny85:
            raise RuntimeError(f"Expected ATtiny85 signature {EXPECTED_SIGNATURE}, received {state.signature or 'no signature'}.")
        self._progress(progress_start + int(span * 0.18), "Backing up flash", "Reading all 8,192 flash bytes")
        flash, flash_results, backend = self._read_memory_with_fallback("flash", FLASH_SIZE)
        self._progress(progress_start + int(span * 0.55), "Backing up EEPROM", "Reading all 512 EEPROM bytes")
        eeprom, eeprom_results, _ = self._read_memory_with_fallback("eeprom", EEPROM_SIZE, preferred=backend)
        all_results = state_results + flash_results + eeprom_results
        if not all(result.ok for result in flash_results[-1:] + eeprom_results[-1:]):
            raise RuntimeError("The automatic backup could not read all device memories. The write was cancelled.")
        self._progress(progress_start + int(span * 0.85), "Saving backup package", "Storing flash, EEPROM, fuses, lock byte, hashes, and logs")
        raw_log = "\n\n".join(result.output for result in all_results)
        package = create_backup_package(flash, eeprom, state, backend, operation, raw_log)
        self._log(f"Automatic backup created: {package}")
        self._progress(progress_end, "Backup complete", str(package))
        return package, state, flash, eeprom, all_results

    def _prepare_memory_write(
        self,
        operation: str,
        create_backup: bool,
        need_flash: bool = False,
        need_eeprom: bool = False,
    ) -> Tuple[Optional[Path], DeviceState, bytes, bytes, List[RunResult]]:
        if create_backup:
            package, state, flash, eeprom, results = self._full_backup(operation, 4, 43)
            return package, state, flash, eeprom, results

        self._progress(5, "Checking target", "Backup is disabled; only required source data will be read")
        state, state_results = self._detect_quiet()
        if not state.is_attiny85:
            raise RuntimeError(f"Expected ATtiny85 signature {EXPECTED_SIGNATURE}, received {state.signature or 'no signature'}.")
        results = list(state_results)
        flash = bytes([0xFF]) * FLASH_SIZE
        eeprom = bytes([0xFF]) * EEPROM_SIZE
        backend: Optional[Backend] = None
        if need_flash:
            self._progress(20, "Reading current flash", "Required for merge or preserved-region handling")
            flash, flash_results, backend = self._read_memory_with_fallback("flash", FLASH_SIZE)
            results.extend(flash_results)
            if not flash_results[-1].ok:
                raise RuntimeError("Current flash could not be read. The write was cancelled.")
        if need_eeprom:
            self._progress(28, "Reading current EEPROM", "Required so EEPROM can be restored after flash programming")
            eeprom, eeprom_results, _ = self._read_memory_with_fallback("eeprom", EEPROM_SIZE, preferred=backend)
            results.extend(eeprom_results)
            if not eeprom_results[-1].ok:
                raise RuntimeError("Current EEPROM could not be read. The write was cancelled.")
        self._progress(35, "Write preparation complete", "No backup package was created")
        return None, state, flash, eeprom, results

    def backup_now(self) -> OperationOutcome:
        package, _state, _flash, _eeprom, results = self._full_backup("manual_backup", 5, 95)
        self._progress(100, "Backup complete", str(package))
        return OperationOutcome(True, "Backup complete", f"Saved complete ATtiny85 backup to {package}", results, backup_path=str(package))

    @staticmethod
    def _save_memory_file(data: bytes, path: Path) -> None:
        if path.suffix.lower() in (".hex", ".ihx"):
            write_intel_hex(bytes_to_memory(data), path)
        else:
            path.write_bytes(data)

    def read_memory(self, memory: str) -> OperationOutcome:
        if memory not in ("flash", "eeprom"):
            raise ValueError("Memory must be flash or eeprom")
        size = FLASH_SIZE if memory == "flash" else EEPROM_SIZE
        page_size = FLASH_PAGE_SIZE if memory == "flash" else EEPROM_PAGE_SIZE
        self._progress(8, f"Preparing {memory} read", "Connecting to the programmer")
        self._progress(20, f"Reading {memory}", f"Transferring {size} bytes from the ATtiny85")
        try:
            data, results, backend = self._read_memory_with_fallback(
                memory, size, progress_callback=self._transfer_progress(20, 82, f"Reading {memory}"),
            )
        except TypeError as exc:
            if "progress_callback" not in str(exc):
                raise
            data, results, backend = self._read_memory_with_fallback(memory, size)
        if not results[-1].ok:
            self._progress(100, f"{memory.title()} read failed", results[-1].classification)
            return OperationOutcome(False, f"{memory.title()} read failed", results[-1].output, results)
        self._progress(88, "Analyzing readback", "Calculating capacity, ranges, page use, and hash")
        stats = analyze_memory(data, page_size)
        self._progress(100, f"{memory.title()} read complete", f"{stats.programmed_bytes} programmed bytes")
        return OperationOutcome(
            True,
            f"{memory.title()} read complete",
            f"Read {size} bytes using {backend.name}. {stats.programmed_bytes} byte(s) are programmed.",
            results,
            extra={"data": data, "stats": stats, "backend_name": backend.name},
        )

    def read_flash_to_file(self, destination: Path) -> OperationOutcome:
        outcome = self.read_memory("flash")
        if not outcome.success:
            return outcome
        data = outcome.extra["data"]
        self._save_memory_file(data, destination)
        outcome.title = "Flash read and save complete"
        outcome.detail = f"Saved {len(data)} bytes to {destination}"
        outcome.extra["saved_path"] = str(destination)
        return outcome

    def read_eeprom_to_file(self, destination: Path) -> OperationOutcome:
        outcome = self.read_memory("eeprom")
        if not outcome.success:
            return outcome
        data = outcome.extra["data"]
        self._save_memory_file(data, destination)
        outcome.title = "EEPROM read and save complete"
        outcome.detail = f"Saved {len(data)} bytes to {destination}"
        outcome.extra["saved_path"] = str(destination)
        return outcome

    def write_flash_data(
        self,
        data: bytes,
        create_backup: bool = True,
        verify_after_write: bool = True,
        full_readback: bool = True,
        restore_eeprom: bool = True,
    ) -> OperationOutcome:
        if len(data) != FLASH_SIZE:
            raise ValueError(f"Flash editor image must be exactly {FLASH_SIZE} bytes")
        with tempfile.TemporaryDirectory(prefix="avrx-flash-editor-") as temp_name:
            path = Path(temp_name) / "edited-flash.hex"
            write_intel_hex(bytes_to_memory(data), path)
            outcome = self.write_flash(
                path,
                offset=0,
                merge_with_current=False,
                preserve_start=None,
                restore_eeprom=restore_eeprom,
                create_backup=create_backup,
                verify_after_write=verify_after_write,
                full_readback=full_readback,
            )
            outcome.extra["source"] = "file/editor buffer"
            return outcome

    def write_eeprom_data(
        self,
        data: bytes,
        create_backup: bool = True,
        verify_after_write: bool = True,
        full_readback: bool = True,
    ) -> OperationOutcome:
        if len(data) != EEPROM_SIZE:
            raise ValueError(f"EEPROM editor image must be exactly {EEPROM_SIZE} bytes")
        with tempfile.TemporaryDirectory(prefix="avrx-eeprom-editor-") as temp_name:
            path = Path(temp_name) / "edited-eeprom.bin"
            path.write_bytes(data)
            outcome = self.write_eeprom(
                path,
                offset=0,
                merge_with_current=False,
                create_backup=create_backup,
                verify_after_write=verify_after_write,
                full_readback=full_readback,
            )
            outcome.extra["source"] = "file/editor buffer"
            return outcome

    def write_eeprom_smart_data(
        self,
        data: bytes,
        create_backup: bool = True,
        verify_after_write: bool = True,
        full_readback: bool = False,
    ) -> OperationOutcome:
        """Program only EEPROM pages that differ from a fresh device readback."""
        if len(data) != EEPROM_SIZE:
            raise ValueError(f"EEPROM editor image must be exactly {EEPROM_SIZE} bytes")
        self._progress(2, "Preparing smart EEPROM write", "Reading the current EEPROM so unchanged pages can be skipped")
        package, _state, _flash, current, results = self._prepare_memory_write(
            "before_smart_eeprom_write",
            create_backup=create_backup,
            need_eeprom=True,
        )
        changed_pages = []
        for page in range(0, EEPROM_SIZE, EEPROM_PAGE_SIZE):
            if current[page:page + EEPROM_PAGE_SIZE] != data[page:page + EEPROM_PAGE_SIZE]:
                changed_pages.append(page // EEPROM_PAGE_SIZE)
        if not changed_pages:
            self._progress(100, "EEPROM already matches", "No EEPROM pages need programming")
            return OperationOutcome(
                True,
                "EEPROM already matches buffer",
                "A fresh EEPROM readback matched all 512 requested bytes. No write was performed."
                + (f" Backup: {package}." if package else ""),
                results,
                backup_path=str(package or ""),
                extra={
                    "memory": "eeprom", "target_data": data, "readback": current,
                    "actual_readback": True, "verified": True, "changed_pages": 0,
                },
            )

        sparse = {}
        for page in changed_pages:
            start = page * EEPROM_PAGE_SIZE
            for address in range(start, start + EEPROM_PAGE_SIZE):
                sparse[address] = data[address]
        self._progress(
            48,
            "Building changed-page image",
            f"{len(changed_pages)} of {EEPROM_SIZE // EEPROM_PAGE_SIZE} EEPROM pages differ",
        )
        with tempfile.TemporaryDirectory(prefix="avrx-eeprom-smart-") as temp_name:
            path = Path(temp_name) / "changed-pages.hex"
            write_intel_hex(sparse, path)
            backend = self._preferred()
            self._progress(58, "Programming changed EEPROM pages", f"Writing {len(sparse)} bytes in {len(changed_pages)} pages")
            write_result = self.runner.write_memory(
                backend,
                "eeprom",
                path,
                "i",
                timeout=self._memory_timeout("eeprom", "write"),
                log_callback=self._log,
                disable_auto_verify=not verify_after_write,
                progress_callback=self._transfer_progress(58, 82, "Programming changed EEPROM pages"),
            )
            results.append(write_result)

        if not write_result.ok:
            self._progress(84, "Reading EEPROM after failed smart write", "Checking what reached the chip")
            readback, diagnostic_results, _ = self._read_memory_with_fallback(
                "eeprom", EEPROM_SIZE, preferred=self._fallback(),
                progress_callback=self._transfer_progress(84, 97, "Diagnosing EEPROM"),
            )
            results.extend(diagnostic_results)
            mismatches = [index for index, (expected, actual) in enumerate(zip(data, readback)) if expected != actual]
            if diagnostic_results[-1].ok and not mismatches:
                self._progress(100, "Smart EEPROM write recovered", "Final readback matched all 512 bytes")
                return OperationOutcome(
                    True,
                    "EEPROM programmed and recovered by readback",
                    f"AVRDUDE did not report normal completion, but all 512 bytes matched. Changed pages: {len(changed_pages)}."
                    + (f" Backup: {package}." if package else ""),
                    results,
                    backup_path=str(package or ""),
                    extra={
                        "memory": "eeprom", "target_data": data, "readback": readback,
                        "actual_readback": True, "verified": True,
                        "changed_pages": len(changed_pages), "recovered_after_error": True,
                    },
                )
            first = mismatches[0] if mismatches else None
            detail = write_result.output
            if diagnostic_results[-1].ok:
                detail += f"\n\nPost-failure readback: {EEPROM_SIZE - len(mismatches)} of {EEPROM_SIZE} bytes match."
                if first is not None:
                    detail += f" First mismatch: 0x{first:04X}."
            if package:
                detail += f"\nBackup: {package}"
            self._progress(100, "Smart EEPROM write failed", write_result.classification)
            return OperationOutcome(
                False,
                "EEPROM programming failed",
                detail,
                results,
                backup_path=str(package or ""),
                mismatch_count=len(mismatches),
                extra={
                    "memory": "eeprom", "target_data": data, "readback": readback,
                    "actual_readback": bool(diagnostic_results[-1].ok), "retryable": True,
                    "first_mismatch": first, "changed_pages": len(changed_pages),
                },
            )

        readback = b""
        if full_readback:
            self._progress(84, "Final full EEPROM readback", "Comparing all 512 bytes")
            readback, verify_results, _ = self._read_memory_with_fallback(
                "eeprom", EEPROM_SIZE, preferred=backend,
                progress_callback=self._transfer_progress(84, 97, "Reading EEPROM back"),
            )
            results.extend(verify_results)
            mismatches = [index for index, (expected, actual) in enumerate(zip(data, readback)) if expected != actual]
            if not verify_results[-1].ok or mismatches:
                first = mismatches[0] if mismatches else None
                return OperationOutcome(
                    False,
                    "Smart EEPROM verification failed",
                    f"Final readback did not match. First mismatch: {('unknown' if first is None else f'0x{first:04X}')}."
                    + (f" Backup: {package}." if package else ""),
                    results,
                    backup_path=str(package or ""),
                    mismatch_count=len(mismatches),
                    extra={"memory": "eeprom", "target_data": data, "readback": readback, "actual_readback": True},
                )

        verification_text = "AVRDUDE verified every changed page" if verify_after_write else "write verification was disabled"
        if full_readback:
            verification_text += "; the complete 512-byte readback also matched"
        self._progress(100, "Smart EEPROM write complete", f"{len(changed_pages)} changed EEPROM pages programmed")
        return OperationOutcome(
            True,
            "EEPROM changed pages programmed and verified" if verify_after_write else "EEPROM changed pages programmed",
            f"Programmed {len(changed_pages)} of 128 EEPROM pages ({len(sparse)} bytes total; 4 bytes per page). {verification_text}."
            + (f" Backup: {package}." if package else ""),
            results,
            backup_path=str(package or ""),
            extra={
                "memory": "eeprom", "target_data": data, "readback": readback,
                "actual_readback": bool(readback), "verified": bool(verify_after_write or full_readback),
                "changed_pages": len(changed_pages),
            },
        )

    def blank_check(self, memory: str) -> OperationOutcome:
        size = FLASH_SIZE if memory == "flash" else EEPROM_SIZE
        self._progress(12, f"Reading {memory} for blank check", f"Checking all {size} bytes")
        data, results, _ = self._read_memory_with_fallback(memory, size)
        if not results[-1].ok:
            self._progress(100, "Blank check failed", results[-1].classification)
            return OperationOutcome(False, "Blank check failed", results[-1].output, results)
        self._progress(90, "Checking for programmed bytes", "Blank memory contains only 0xFF")
        programmed = [index for index, value in enumerate(data) if value != 0xFF]
        if programmed:
            self._progress(100, f"{memory.title()} is not blank", f"First programmed address 0x{programmed[0]:04X}")
            return OperationOutcome(
                False,
                f"{memory.title()} is not blank",
                f"Found {len(programmed)} programmed bytes. First programmed address: 0x{programmed[0]:04X}",
                results,
                mismatch_count=len(programmed),
            )
        self._progress(100, f"{memory.title()} is blank", "Every byte is 0xFF")
        return OperationOutcome(True, f"{memory.title()} is blank", "Every byte reads as 0xFF.", results)

    def verify_image(self, memory: str, file_path: Path, offset: int = 0) -> OperationOutcome:
        size = FLASH_SIZE if memory == "flash" else EEPROM_SIZE
        self._progress(10, "Preparing comparison", f"Loading addressed bytes from {file_path.name}")
        _, payload = load_image(file_path, size, offset=offset)
        self._progress(25, f"Reading {memory} from chip", f"Only the file's {len(payload)} addressed bytes will be compared")
        actual, results, _ = self._read_memory_with_fallback(memory, size)
        if not results[-1].ok:
            self._progress(100, "Comparison read failed", results[-1].classification)
            return OperationOutcome(False, "Comparison read failed", results[-1].output, results)
        self._progress(90, "Comparing chip to selected file", "This operation does not write anything")
        mismatches = compare_payload(actual, payload)
        if mismatches:
            preview = "\n".join(
                f"0x{address:04X}: expected 0x{expected:02X}, read " + ("outside memory" if actual_value < 0 else f"0x{actual_value:02X}")
                for address, expected, actual_value in mismatches[:20]
            )
            self._progress(100, "Comparison failed", f"{len(mismatches)} mismatched bytes")
            return OperationOutcome(
                False,
                "Chip does not match selected file",
                f"{len(mismatches)} addressed byte(s) differ.\n{preview}",
                results,
                mismatch_count=len(mismatches),
                extra={"memory": memory, "readback": actual, "actual_readback": True},
            )
        self._progress(100, "Chip matches selected file", f"All {len(payload)} addressed bytes match")
        return OperationOutcome(
            True,
            "Chip matches selected file",
            f"All {len(payload)} addressed bytes in the selected file match the chip. Nothing was written.",
            results,
            extra={"memory": memory, "readback": actual, "actual_readback": True},
        )

    def verify_data(self, memory: str, expected: bytes) -> OperationOutcome:
        size = FLASH_SIZE if memory == "flash" else EEPROM_SIZE
        if len(expected) != size:
            raise ValueError(f"{memory.title()} editor buffer must be exactly {size} bytes")
        self._progress(10, "Preparing buffer comparison", f"Comparing all {size} editor-buffer bytes")
        actual, results, _ = self._read_memory_with_fallback(
            memory,
            size,
            progress_callback=self._transfer_progress(20, 86, f"Reading {memory}"),
        )
        if not results[-1].ok:
            self._progress(100, "Comparison read failed", results[-1].classification)
            return OperationOutcome(False, "Comparison read failed", results[-1].output, results)
        mismatches = [
            index for index, (wanted, found) in enumerate(zip(expected, actual))
            if wanted != found
        ]
        if mismatches:
            preview = "\n".join(
                f"0x{address:04X}: buffer 0x{expected[address]:02X}, chip 0x{actual[address]:02X}"
                for address in mismatches[:20]
            )
            self._progress(100, "Buffer comparison failed", f"{len(mismatches)} byte(s) differ")
            return OperationOutcome(
                False,
                "Chip does not match editor buffer",
                f"{len(mismatches)} of {size} byte(s) differ.\n{preview}",
                results,
                mismatch_count=len(mismatches),
                extra={
                    "memory": memory,
                    "readback": actual,
                    "actual_readback": True,
                    "first_mismatch": mismatches[0],
                },
            )
        self._progress(100, "Chip matches editor buffer", f"All {size} bytes match")
        return OperationOutcome(
            True,
            "Chip matches editor buffer",
            f"All {size} bytes in the current editor buffer match the connected chip. Nothing was written.",
            results,
            extra={"memory": memory, "readback": actual, "actual_readback": True},
        )

    def write_flash(
        self,
        file_path: Path,
        offset: int = 0,
        merge_with_current: bool = False,
        preserve_start: Optional[int] = None,
        restore_eeprom: bool = True,
        create_backup: bool = True,
        verify_after_write: bool = True,
        full_readback: bool = True,
    ) -> OperationOutcome:
        # Restoring EEPROM requires a pre-write EEPROM read only when EESAVE is not programmed.
        self._progress(2, "Preparing flash operation", "Checking selected safeguards and source image")
        need_flash = merge_with_current or preserve_start is not None
        package, state, current_flash, current_eeprom, results = self._prepare_memory_write(
            "before_flash_write",
            create_backup=create_backup,
            need_flash=need_flash,
            need_eeprom=False,
        )
        eeprom_may_be_erased = state.hfuse is not None and ((state.hfuse >> 3) & 1) == 1
        if restore_eeprom and eeprom_may_be_erased and not create_backup:
            self._progress(36, "Reading EEPROM for preservation", "EESAVE is off, so flash programming may erase EEPROM")
            current_eeprom, eeprom_results, _ = self._read_memory_with_fallback("eeprom", EEPROM_SIZE)
            results.extend(eeprom_results)
            if not eeprom_results[-1].ok:
                raise RuntimeError("EEPROM preservation was requested, but EEPROM could not be read. The flash write was cancelled.")

        base = current_flash if merge_with_current else bytes([0xFF]) * FLASH_SIZE
        target, payload = load_image(file_path, FLASH_SIZE, offset=offset, base=base)
        if preserve_start is not None:
            if not 0 <= preserve_start < FLASH_SIZE:
                raise ValueError("Bootloader preservation start must be inside flash memory.")
            target = target[:preserve_start] + current_flash[preserve_start:]
            payload = {address: value for address, value in payload.items() if address < preserve_start}

        self._progress(48, "Building flash image", f"Resolved {len(payload)} addressed bytes into an 8192-byte target image")
        with tempfile.TemporaryDirectory(prefix="avrx-write-") as temp_name:
            image_path = Path(temp_name) / "flash.hex"
            write_intel_hex(bytes_to_memory(target), image_path)
            backend = self._preferred()
            self._progress(58, "Programming flash", "AVRDUDE is erasing and writing flash pages")
            write_result = self.runner.write_memory(
                backend,
                "flash",
                image_path,
                "i",
                timeout=self._memory_timeout("flash", "write"),
                log_callback=self._log,
                disable_auto_verify=not verify_after_write,
                progress_callback=self._transfer_progress(58, 78, "Programming flash"),
            )
            results.append(write_result)

        if not write_result.ok:
            self._progress(100, "Flash programming failed", write_result.classification)
            backup_text = f" Backup: {package}" if package else ""
            return OperationOutcome(False, "Flash programming failed", write_result.output + backup_text, results, backup_path=str(package or ""))

        readback = b""
        if full_readback:
            self._progress(79, "Full flash readback", "Reading all 8192 bytes for a second byte-for-byte comparison")
            verify_backend = backend if write_result.ok else self._fallback()
            readback, verify_results, _ = self._read_memory_with_fallback(
                "flash", FLASH_SIZE, preferred=verify_backend,
                progress_callback=self._transfer_progress(79, 87, "Reading flash back"),
            )
            results.extend(verify_results)
            mismatches = [index for index, (expected, actual) in enumerate(zip(target, readback)) if expected != actual]
            if mismatches:
                self._progress(100, "Full readback mismatch", f"First mismatch 0x{mismatches[0]:04X}")
                return OperationOutcome(
                    False,
                    "Flash write could not be verified",
                    f"{len(mismatches)} byte(s) differ after full readback. First mismatch: 0x{mismatches[0]:04X}." + (f" Backup: {package}" if package else ""),
                    results,
                    backup_path=str(package or ""),
                    mismatch_count=len(mismatches),
                    extra={"memory": "flash", "target_data": target, "readback": readback, "actual_readback": True},
                )

        eeprom_restored = False
        if restore_eeprom and eeprom_may_be_erased:
            self._progress(88, "Restoring EEPROM", "Writing back the EEPROM image saved before flash programming")
            with tempfile.TemporaryDirectory(prefix="avrx-eeprom-restore-") as temp_name:
                eeprom_path = Path(temp_name) / "eeprom.bin"
                eeprom_path.write_bytes(current_eeprom)
                restore_result = self.runner.write_memory(
                    self._fallback(),
                    "eeprom",
                    eeprom_path,
                    "r",
                    timeout=self._memory_timeout("eeprom", "write"),
                    log_callback=self._log,
                    disable_auto_verify=not verify_after_write,
                    progress_callback=self._transfer_progress(88, 96, "Restoring EEPROM"),
                )
                results.append(restore_result)
                if not restore_result.ok:
                    return OperationOutcome(
                        False,
                        "Flash programmed, but EEPROM restoration failed",
                        restore_result.output + (f"\nBackup: {package}" if package else ""),
                        results,
                        backup_path=str(package or ""),
                    )
                eeprom_restored = True

        verification_text = (
            "AVRDUDE write verification passed"
            if verify_after_write
            else "WARNING: write verification was disabled"
        )
        if full_readback:
            verification_text += "; a separate full 8192-byte readback also matched"
        backup_text = f"Backup: {package}" if package else "Backup was disabled for this operation"
        detail = f"Flash programming completed. {verification_text}. {backup_text}."
        if eeprom_restored:
            detail += " EEPROM was restored because EESAVE was not programmed."
        if write_result.classification == "soft_success":
            detail += " AVRDUDE crashed during shutdown after reporting completion."
        self._progress(100, "Flash operation complete", verification_text)
        return OperationOutcome(
            True,
            "Flash programmed" if not verify_after_write else "Flash programmed and verified",
            detail,
            results,
            backup_path=str(package or ""),
            extra={
                "memory": "flash",
                "target_data": target,
                "readback": readback,
                "actual_readback": bool(readback),
                "verified": bool(verify_after_write or full_readback),
            },
        )

    def write_eeprom(
        self,
        file_path: Path,
        offset: int = 0,
        merge_with_current: bool = True,
        create_backup: bool = True,
        verify_after_write: bool = True,
        full_readback: bool = True,
    ) -> OperationOutcome:
        self._progress(2, "Preparing EEPROM operation", "Checking selected safeguards and source image")
        package, _state, _flash, current_eeprom, results = self._prepare_memory_write(
            "before_eeprom_write",
            create_backup=create_backup,
            need_eeprom=merge_with_current,
        )
        base = current_eeprom if merge_with_current else bytes([0xFF]) * EEPROM_SIZE
        target, _payload = load_image(file_path, EEPROM_SIZE, offset=offset, base=base)
        self._progress(48, "Building EEPROM image", "Resolving the selected file into a complete 512-byte target")
        with tempfile.TemporaryDirectory(prefix="avrx-eeprom-write-") as temp_name:
            image_path = Path(temp_name) / "eeprom.bin"
            image_path.write_bytes(target)
            backend = self._preferred()
            self._progress(60, "Programming EEPROM", "Writing EEPROM pages")
            write_result = self.runner.write_memory(
                backend,
                "eeprom",
                image_path,
                "r",
                timeout=self._memory_timeout("eeprom", "write"),
                log_callback=self._log,
                disable_auto_verify=not verify_after_write,
                progress_callback=self._transfer_progress(60, 80, "Programming EEPROM"),
            )
            results.append(write_result)
        if not write_result.ok:
            can_diagnose = (
                write_result.timed_out
                or write_result.verification_error
                or bool(write_result.signature)
                or "writing eeprom" in write_result.output.lower()
            )
            diagnostic = b""
            mismatches = []
            if can_diagnose:
                self._progress(82, "Reading EEPROM after failed write", "Checking exactly what reached the chip")
                diagnostic, diagnostic_results, _ = self._read_memory_with_fallback(
                    "eeprom", EEPROM_SIZE, preferred=self._fallback(),
                    progress_callback=self._transfer_progress(82, 96, "Diagnosing EEPROM"),
                )
                results.extend(diagnostic_results)
                if diagnostic_results[-1].ok:
                    mismatches = [
                        index for index, (expected, actual) in enumerate(zip(target, diagnostic))
                        if expected != actual
                    ]
                    if not mismatches:
                        verification_text = (
                            "AVRDUDE did not report normal completion, but the mandatory post-failure "
                            "readback matched all 512 target bytes."
                        )
                        self._progress(100, "EEPROM write recovered", "All 512 bytes matched on readback")
                        return OperationOutcome(
                            True,
                            "EEPROM programmed and recovered by readback",
                            verification_text + (f" Backup: {package}." if package else ""),
                            results,
                            backup_path=str(package or ""),
                            extra={
                                "memory": "eeprom", "target_data": target, "readback": diagnostic,
                                "actual_readback": True, "verified": True, "recovered_after_error": True,
                            },
                        )

            detail = write_result.output
            extra = {"memory": "eeprom", "target_data": target}
            mismatch_count = 0
            if diagnostic:
                mismatch_count = len(mismatches)
                matched = EEPROM_SIZE - mismatch_count
                first = mismatches[0] if mismatches else None
                detail += (
                    f"\n\nPost-failure readback: {matched} of {EEPROM_SIZE} bytes match the requested image."
                )
                if first is not None:
                    detail += f" First mismatch: 0x{first:04X}."
                detail += " The DEVICE pane was refreshed with the actual EEPROM contents."
                extra.update({
                    "readback": diagnostic, "actual_readback": True,
                    "retryable": True, "first_mismatch": first,
                })
            if write_result.timed_out:
                detail += (
                    "\n\nThis was an ATtiny85 Explorer timeout, not a completed AVRDUDE verification result. "
                    "Check target power and ISP wiring, then retry the same image."
                )
            if package:
                detail += f"\nBackup: {package}"
            self._progress(100, "EEPROM programming failed", write_result.classification)
            return OperationOutcome(
                False, "EEPROM programming failed", detail, results,
                backup_path=str(package or ""), mismatch_count=mismatch_count, extra=extra,
            )

        readback = b""
        if full_readback:
            self._progress(82, "Full EEPROM readback", "Reading all 512 bytes for a second comparison")
            readback, verify_results, _ = self._read_memory_with_fallback(
                "eeprom", EEPROM_SIZE, preferred=backend,
                progress_callback=self._transfer_progress(82, 96, "Reading EEPROM back"),
            )
            results.extend(verify_results)
            mismatches = [index for index, (expected, actual) in enumerate(zip(target, readback)) if expected != actual]
            if mismatches:
                self._progress(100, "Full readback mismatch", f"First mismatch 0x{mismatches[0]:04X}")
                return OperationOutcome(
                    False,
                    "EEPROM write could not be verified",
                    f"{len(mismatches)} byte(s) differ after full readback. First mismatch: 0x{mismatches[0]:04X}." + (f" Backup: {package}" if package else ""),
                    results,
                    backup_path=str(package or ""),
                    mismatch_count=len(mismatches),
                    extra={"memory": "eeprom", "target_data": target, "readback": readback, "actual_readback": True},
                )

        verification_text = "AVRDUDE write verification passed" if verify_after_write else "WARNING: write verification was disabled"
        if full_readback:
            verification_text += "; a separate full 512-byte readback also matched"
        backup_text = f"Backup: {package}" if package else "Backup was disabled for this operation"
        self._progress(100, "EEPROM operation complete", verification_text)
        return OperationOutcome(
            True,
            "EEPROM programmed" if not verify_after_write else "EEPROM programmed and verified",
            f"EEPROM programming completed. {verification_text}. {backup_text}.",
            results,
            backup_path=str(package or ""),
            extra={
                "memory": "eeprom",
                "target_data": target,
                "readback": readback,
                "actual_readback": bool(readback),
                "verified": bool(verify_after_write or full_readback),
            },
        )

    def erase_flash(self, preserve_eeprom: bool = True) -> OperationOutcome:
        """Erase program flash with the AVR chip-erase command and reread both memories.

        AVR devices do not provide a separate ISP command that only turns flash
        bytes back into 0xFF. Chip erase is required, which also clears lock bits
        and can erase EEPROM. When preservation is requested, ATtiny85 Explorer uses
        its mandatory pre-erase backup and restores EEPROM only if chip erase
        actually changed it. Fuse bytes are never rewritten by this operation.
        """
        self._progress(2, "Preparing flash erase", "Backing up the ATtiny85 before chip erase")
        backup, previous_state, _old_flash, saved_eeprom, results = self._full_backup(
            "before_flash_erase", 4, 42
        )
        self._progress(48, "Erasing flash", "Chip erase clears flash and lock bits; fuse bytes stay unchanged")
        erase_result = self.runner.chip_erase(
            self._preferred(), timeout=max(90, self._timeout()), log_callback=self._log
        )
        results.append(erase_result)
        if not erase_result.ok:
            self._progress(100, "Flash erase failed", erase_result.classification)
            return OperationOutcome(
                False, "Flash erase failed",
                erase_result.output + f"\nBackup: {backup}", results, backup_path=str(backup or ""),
            )

        self._progress(58, "Reading erased flash", "Refreshing the Flash DEVICE pane")
        flash, flash_results, backend = self._read_memory_with_fallback(
            "flash", FLASH_SIZE, preferred=self._fallback(),
            progress_callback=self._transfer_progress(58, 74, "Reading erased flash"),
        )
        results.extend(flash_results)
        if not flash_results[-1].ok:
            return OperationOutcome(
                False, "Flash erased, but readback failed",
                flash_results[-1].output + f"\nBackup: {backup}", results,
                backup_path=str(backup or ""),
            )

        self._progress(76, "Reading EEPROM after chip erase", "Checking whether EEPROM was preserved")
        eeprom, eeprom_results, _ = self._read_memory_with_fallback(
            "eeprom", EEPROM_SIZE, preferred=backend,
            progress_callback=self._transfer_progress(76, 84, "Reading EEPROM"),
        )
        results.extend(eeprom_results)
        if not eeprom_results[-1].ok:
            return OperationOutcome(
                False, "Flash erased, but EEPROM status could not be read",
                eeprom_results[-1].output + f"\nBackup: {backup}", results,
                backup_path=str(backup),
                extra={"memory": "flash", "readback": flash, "actual_readback": True},
            )

        eeprom_restored = False
        if preserve_eeprom and eeprom != saved_eeprom:
            self._progress(85, "Restoring EEPROM", "Chip erase changed EEPROM; restoring the saved 512-byte image")
            with tempfile.TemporaryDirectory(prefix="avrx-flash-erase-eeprom-") as temp_name:
                eeprom_path = Path(temp_name) / "eeprom.bin"
                eeprom_path.write_bytes(saved_eeprom)
                restore_result = self.runner.write_memory(
                    self._fallback(), "eeprom", eeprom_path, "r",
                    timeout=self._memory_timeout("eeprom", "write"),
                    log_callback=self._log,
                    progress_callback=self._transfer_progress(85, 94, "Restoring EEPROM"),
                )
                results.append(restore_result)
            if not restore_result.ok:
                return OperationOutcome(
                    False, "Flash erased, but EEPROM restoration failed",
                    restore_result.output + f"\nBackup: {backup}", results,
                    backup_path=str(backup),
                    extra={
                        "memory": "flash", "readback": flash, "actual_readback": True,
                        "eeprom_readback": eeprom,
                    },
                )
            eeprom, verify_results, _ = self._read_memory_with_fallback(
                "eeprom", EEPROM_SIZE, preferred=self._fallback(),
                progress_callback=self._transfer_progress(94, 97, "Verifying restored EEPROM"),
            )
            results.extend(verify_results)
            eeprom_restored = bool(verify_results[-1].ok and eeprom == saved_eeprom)

        self._progress(97, "Checking lock bits", "Confirming chip erase cleared programming protection")
        final_state, state_results = self._detect_quiet(self._fallback())
        results.extend(state_results)
        blank = all(value == 0xFF for value in flash)
        unlocked = final_state.lock is not None and (final_state.lock & 0x03) == 0x03
        previous_fuses = (previous_state.lfuse, previous_state.hfuse, previous_state.efuse)
        final_fuses = (final_state.lfuse, final_state.hfuse, final_state.efuse)
        fuses_unchanged = previous_fuses == final_fuses
        eeprom_ok = (not preserve_eeprom) or eeprom == saved_eeprom
        success = blank and unlocked and fuses_unchanged and eeprom_ok
        if success:
            eeprom_text = (
                "EEPROM was restored from the backup" if eeprom_restored
                else "EEPROM already matched the backup and did not need rewriting"
            ) if preserve_eeprom else "EEPROM preservation was not requested"
            self._progress(100, "Flash erase verified", "Flash is all 0xFF and both DEVICE panes are current")
            return OperationOutcome(
                True, "Flash erased and reread",
                f"Flash is blank, lock bits are cleared, and LFUSE/HFUSE/EFUSE matched their pre-erase values. {eeprom_text}. Backup: {backup}.",
                results, backup_path=str(backup),
                extra={
                    "memory": "flash", "readback": flash, "actual_readback": True,
                    "eeprom_readback": eeprom, "eeprom_preserved": eeprom_ok,
                },
            )

        self._progress(
            100,
            "Flash erase verification failed",
            f"blank={blank}, unlocked={unlocked}, fuses unchanged={fuses_unchanged}, EEPROM preserved={eeprom_ok}",
        )
        return OperationOutcome(
            False, "Flash erase could not be fully verified",
            f"Flash blank={blank}; lock bits cleared={unlocked}; fuses unchanged={fuses_unchanged}; EEPROM preserved={eeprom_ok}. Backup: {backup}.",
            results, backup_path=str(backup),
            extra={
                "memory": "flash", "readback": flash, "actual_readback": True,
                "eeprom_readback": eeprom,
            },
        )

    def erase_eeprom(
        self,
        create_backup: bool = True,
        verify_after_write: bool = True,
        full_readback: bool = True,
    ) -> OperationOutcome:
        with tempfile.TemporaryDirectory(prefix="avrx-eeprom-erase-") as temp_name:
            path = Path(temp_name) / "blank.bin"
            path.write_bytes(bytes([0xFF]) * EEPROM_SIZE)
            return self.write_eeprom(
                path,
                merge_with_current=False,
                create_backup=create_backup,
                verify_after_write=verify_after_write,
                full_readback=full_readback,
            )

    def write_fuses(
        self,
        config: FuseConfig,
        raw_values: Optional[Tuple[int, int, int]] = None,
        create_backup: bool = True,
    ) -> OperationOutcome:
        if create_backup:
            self._progress(2, "Preparing fuse write", "Creating a complete safety backup before hardware fuse bytes change")
            backup, state, _flash, _eeprom, results = self._full_backup("before_fuse_write")
        else:
            self._progress(5, "Preparing fuse write", "Using the safety backup already created for this grouped operation")
            state, results = self._detect_quiet()
            backup = None
            if not state.is_attiny85:
                raise RuntimeError(f"Expected ATtiny85 signature {EXPECTED_SIGNATURE}, received {state.signature or 'no signature'}.")
        target = raw_values if raw_values is not None else config.encode()
        target_l, target_h, target_e = target
        current = {"lfuse": state.lfuse, "hfuse": state.hfuse, "efuse": state.efuse}
        proposed = {"lfuse": target_l, "hfuse": target_h, "efuse": target_e}

        # EFUSE and ordinary HFUSE changes first; clock-related LFUSE next;
        # RESET/debug changes last so they cannot block the earlier writes.
        hfuse_is_dangerous = (target_h & 0xC0) != 0xC0
        order = ["efuse"]
        if not hfuse_is_dangerous:
            order.append("hfuse")
        order.append("lfuse")
        if hfuse_is_dangerous:
            order.append("hfuse")
        backend = self._preferred()
        changed_names = [name for name in order if current[name] is None or current[name] != proposed[name]]
        for index, name in enumerate(changed_names):
            percent = 78 + int((index / max(1, len(changed_names))) * 10)
            self._progress(percent, f"Writing {name.upper()}", f"Programming 0x{proposed[name]:02X}; clock-related fuses are ordered carefully")
            result = self.runner.write_fuse(
                backend, name, proposed[name], timeout=self._timeout(), log_callback=self._log
            )
            results.append(result)
            if not result.ok:
                # Do not blindly repeat a fuse write. Read back with known-good backend.
                self._log("Fuse write backend reported an error; switching to readback recovery.")
                break

        self._progress(92, "Reading fuses back", "The exact LFUSE, HFUSE, and EFUSE bytes must match")
        final_state, state_results = self._detect_quiet(self._fallback())
        results.extend(state_results)
        if final_state.lfuse == target_l and final_state.hfuse == target_h and final_state.efuse == target_e:
            self._progress(100, "Fuse write verified", f"0x{target_l:02X} / 0x{target_h:02X} / 0x{target_e:02X}")
            return OperationOutcome(
                True,
                "Fuse write verified",
                f"LFUSE=0x{target_l:02X}, HFUSE=0x{target_h:02X}, EFUSE=0x{target_e:02X}. " + (f"Backup: {backup}" if backup else "Grouped-operation backup was created earlier"),
                results,
                backup_path=str(backup or ""),
            )

        effective_config = FuseConfig.decode(target_l, target_h, target_e) if raw_values is not None else config
        critical = effective_config.reset_disabled or effective_config.clock_source.startswith("External") or "crystal" in effective_config.clock_source.lower()
        if critical and any(result.completed_marker for result in results):
            self._progress(100, "Fuse readback unavailable", "A high-risk clock or RESET change may already be active")
            return OperationOutcome(
                False,
                "Fuse state could not be read after a high-risk change",
                "A high-risk clock or RESET change may have taken effect and made ISP communication unavailable. "
                "Do not repeat the write blindly. " + (f"Backup: {backup}" if backup else "Use the grouped-operation safety backup."),
                results,
                backup_path=str(backup or ""),
            )
        self._progress(100, "Fuse verification failed", "Readback did not match the requested bytes")
        return OperationOutcome(
            False,
            "Fuse verification failed",
            f"Requested 0x{target_l:02X}/0x{target_h:02X}/0x{target_e:02X}, but readback did not match. " + (f"Backup: {backup}" if backup else "Use the grouped-operation safety backup."),
            results,
            backup_path=str(backup or ""),
        )

    def write_lock(self, value: int, create_backup: bool = True) -> OperationOutcome:
        if value not in (0xFE, 0xFC):
            raise ValueError("Only documented ATtiny85 lock modes 2 and 3 may be programmed.")
        if create_backup:
            self._progress(2, "Preparing lock-bit write", "Creating the required safety backup")
            backup, _state, _flash, _eeprom, results = self._full_backup("before_lock_write", 5, 62)
        else:
            self._progress(8, "Preparing lock-bit write", "Using the safety backup already created for this grouped operation")
            state_before, results = self._detect_quiet()
            backup = None
            if not state_before.is_attiny85:
                raise RuntimeError(f"Expected ATtiny85 signature {EXPECTED_SIGNATURE}, received {state_before.signature or 'no signature'}.")
        self._progress(72, "Writing lock bits", f"Programming lock byte 0x{value:02X}")
        result = self.runner.write_lock(self._preferred(), value, timeout=self._timeout(), log_callback=self._log)
        results.append(result)
        self._progress(88, "Reading lock bits back", "Confirming the selected protection mode")
        state, state_results = self._detect_quiet(self._fallback())
        results.extend(state_results)
        if state.lock is not None and (state.lock & 0x3) == (value & 0x3):
            self._progress(100, "Lock bits verified", f"Lock byte 0x{state.lock:02X}")
            return OperationOutcome(True, "Lock bits verified", f"Lock byte is 0x{state.lock:02X}. " + (f"Backup: {backup}" if backup else "Grouped-operation backup was created earlier"), results, backup_path=str(backup or ""))
        self._progress(100, "Lock verification failed", "Readback did not match")
        return OperationOutcome(False, "Lock-bit verification failed", "Readback did not match. " + (f"Backup: {backup}" if backup else "Use the grouped-operation safety backup."), results, backup_path=str(backup or ""))

    def chip_erase(self) -> OperationOutcome:
        self._progress(2, "Preparing chip erase", "Attempting a complete backup before destructive erase")
        backup = None
        results: List[RunResult] = []
        try:
            backup, _state, _flash, _eeprom, results = self._full_backup("before_chip_erase", 5, 48)
        except Exception as exc:
            # A mode-3 locked device may prohibit the read required for a backup.
            # Chip erase must still remain available as the documented unlock path.
            self._log(f"Pre-erase backup was unavailable: {exc}")
        self._progress(58, "Erasing chip", "Clearing flash and lock bits; fuse bytes are not reset")
        result = self.runner.chip_erase(self._preferred(), timeout=self._timeout(), log_callback=self._log)
        results.append(result)
        self._progress(73, "Checking erased flash", "Reading all 8,192 flash bytes")
        flash, flash_results, _ = self._read_memory_with_fallback("flash", FLASH_SIZE, preferred=self._fallback())
        results.extend(flash_results)
        self._progress(90, "Checking lock bits", "Confirming the device is unlocked")
        state, state_results = self._detect_quiet(self._fallback())
        results.extend(state_results)
        blank = all(value == 0xFF for value in flash)
        unlocked = state.lock is not None and (state.lock & 0x3) == 0x3
        if blank and unlocked:
            self._progress(100, "Chip erase verified", "Flash is blank and lock bits are cleared")
            return OperationOutcome(True, "Chip erase verified", f"Flash is blank and lock bits are cleared. Backup: {backup if backup else 'not available before erase'}", results, backup_path=str(backup) if backup else "")
        self._progress(100, "Chip erase verification failed", f"blank={blank}, unlocked={unlocked}")
        return OperationOutcome(False, "Chip erase could not be fully verified", f"Blank={blank}, unlocked={unlocked}. Backup: {backup if backup else 'not available'}", results, backup_path=str(backup) if backup else "")

    def restore_backup(self, package_path: Path) -> OperationOutcome:
        package = read_backup_package(package_path)
        manifest = package["manifest"]
        if manifest.get("device") != "ATtiny85" or manifest.get("expected_signature") != EXPECTED_SIGNATURE:
            raise ValueError("The selected package is not an ATtiny85 Explorer backup.")
        backup, _state, _flash, _eeprom, results = self._full_backup("before_backup_restore")
        flash = package["flash"]
        eeprom = package["eeprom"]
        fuses = manifest.get("fuses", {})

        with tempfile.TemporaryDirectory(prefix="avrx-restore-") as temp_name:
            temp = Path(temp_name)
            flash_path = temp / "flash.hex"
            eeprom_path = temp / "eeprom.bin"
            write_intel_hex(bytes_to_memory(flash), flash_path)
            eeprom_path.write_bytes(eeprom)
            backend = self._preferred()
            results.append(self.runner.write_memory(
                backend, "flash", flash_path, "i",
                timeout=self._memory_timeout("flash", "write"), log_callback=self._log,
                progress_callback=self._transfer_progress(55, 72, "Restoring flash"),
            ))
            results.append(self.runner.write_memory(
                backend, "eeprom", eeprom_path, "r",
                timeout=self._memory_timeout("eeprom", "write"), log_callback=self._log,
                progress_callback=self._transfer_progress(72, 84, "Restoring EEPROM"),
            ))

        lfuse = fuses.get("lfuse")
        hfuse = fuses.get("hfuse")
        efuse = fuses.get("efuse")
        lock = fuses.get("lock")
        if all(isinstance(value, int) for value in (lfuse, hfuse, efuse)):
            decoded = FuseConfig.decode(lfuse, hfuse, efuse)
            fuse_outcome = self.write_fuses(decoded, raw_values=(lfuse, hfuse, efuse), create_backup=False)
            results.extend(fuse_outcome.run_results)
            if not fuse_outcome.success:
                return OperationOutcome(False, "Memory restored, fuse restoration failed", fuse_outcome.detail, results, backup_path=str(backup))

        read_flash, flash_results, _ = self._read_memory_with_fallback("flash", FLASH_SIZE, preferred=self._fallback())
        read_eeprom, eeprom_results, _ = self._read_memory_with_fallback("eeprom", EEPROM_SIZE, preferred=self._fallback())
        results.extend(flash_results + eeprom_results)
        if read_flash != flash or read_eeprom != eeprom:
            return OperationOutcome(False, "Backup restoration mismatch", f"Final memory did not match the package. Safety backup: {backup}", results, backup_path=str(backup))

        if isinstance(lock, int) and (lock & 0x3) in (0x2, 0x0):
            lock_value = 0xFE if (lock & 0x3) == 0x2 else 0xFC
            lock_outcome = self.write_lock(lock_value, create_backup=False)
            results.extend(lock_outcome.run_results)
            if not lock_outcome.success:
                return OperationOutcome(False, "Memory and fuses restored, lock restoration failed", lock_outcome.detail, results, backup_path=str(backup))

        return OperationOutcome(True, "Backup restored and verified", f"Package restored byte-for-byte. Safety backup: {backup}", results, backup_path=str(backup))

    @staticmethod
    def _parse_selftest_report(eeprom: bytes) -> Dict[str, object]:
        report = eeprom[SELFTEST_REPORT_BASE:SELFTEST_REPORT_BASE + SELFTEST_REPORT_SIZE]
        valid = (
            len(report) == SELFTEST_REPORT_SIZE
            and report[:4] == b"AVRX"
            and report[4] == 1
            and report[11] == 0xA5
            and report[31] == 0x5A
        )
        if not valid:
            return {
                "valid": False,
                "raw": report,
                "detail": "The temporary firmware did not produce a complete ATtiny85 Explorer report.",
            }
        failed_address = report[16] | (report[17] << 8)
        tested_sram = report[14] | (report[15] << 8)
        tests = {
            "CPU arithmetic/control flow": bool(report[5]),
            "SRAM pattern test": bool(report[6]),
            "Timer0 overflow": bool(report[7]),
            "High-speed Timer1 overflow": bool(report[8]),
            "Runtime EEPROM read/write": bool(report[9]),
        }
        lines = [
            "Temporary on-chip test report:",
            *[f"  {name}: {'PASS' if passed else 'FAIL'}" for name, passed in tests.items()],
            f"  SRAM tested: {tested_sram} bytes (0x0060-0x01FF)",
            f"  Reset flags at test start: 0x{report[12]:02X}",
            f"  Factory-loaded OSCCAL register at test start: 0x{report[13]:02X}",
        ]
        if not tests["SRAM pattern test"]:
            lines.append(
                f"  First SRAM mismatch: address 0x{failed_address:04X}; "
                f"expected 0x{report[18]:02X}, read 0x{report[19]:02X}"
            )
        return {
            "valid": True,
            "passed": bool(report[10]) and all(tests.values()),
            "tests": tests,
            "raw": report,
            "detail": "\n".join(lines),
            "reset_flags": report[12],
            "osccal": report[13],
            "sram_tested": tested_sram,
            "failed_address": failed_address,
        }

    def on_chip_self_test(self) -> OperationOutcome:
        """Temporarily replace firmware, run internal tests, then restore byte-for-byte.

        This test requires no external pins or serial adapter. The complete Flash,
        EEPROM, fuse, and lock state is backed up first. The temporary image writes
        a small report into EEPROM; ATtiny85 Explorer reads it and always attempts to
        restore the original device before returning.
        """
        selftest_hex = APP_ROOT / "resources" / "selftest" / "attiny85_selftest.hex"
        if not selftest_hex.exists():
            raise RuntimeError(f"The packaged self-test image is missing: {selftest_hex}")
        actual_hash = hashlib.sha256(selftest_hex.read_bytes()).hexdigest()
        if actual_hash != SELFTEST_HEX_SHA256:
            raise RuntimeError(
                "The packaged self-test image failed its integrity check. "
                "Re-extract ATtiny85 Explorer before running a destructive diagnostic."
            )

        self._progress(2, "Preparing on-chip self-test", "Creating a complete recovery backup")
        backup, original_state, original_flash, original_eeprom, results = self._full_backup(
            "before_on_chip_self_test", 3, 28
        )
        if original_state.lock is not None and (original_state.lock & 0x03) != 0x03:
            return OperationOutcome(
                False,
                "On-chip self-test blocked",
                "The chip is locked. ATtiny85 Explorer will not erase or temporarily replace protected firmware for a diagnostic test. "
                f"Backup: {backup}",
                results,
                backup_path=str(backup),
            )

        report_info: Dict[str, object] = {"valid": False, "detail": "The test did not run."}
        restore_ok = False
        restored_flash = b""
        restored_eeprom = b""
        final_state = DeviceState()
        restore_detail = "Restoration was not attempted."
        test_programmed = False
        try:
            self._progress(32, "Loading temporary self-test", "Chip erase and temporary diagnostic firmware write")
            write_result = self.runner.write_memory(
                self._preferred(),
                "flash",
                selftest_hex,
                "i",
                timeout=self._memory_timeout("flash", "write"),
                log_callback=self._log,
                progress_callback=self._transfer_progress(32, 48, "Loading self-test"),
            )
            results.append(write_result)
            if not write_result.ok:
                report_info = {
                    "valid": False,
                    "detail": "The temporary self-test firmware could not be programmed. No test result is available.",
                }
            else:
                test_programmed = True
                self._progress(52, "Running tests inside ATtiny85", "CPU, SRAM, timers, and runtime EEPROM; no external pins are used")
                # AVRDUDE releases RESET after programming. Even the 128 kHz clock
                # completes this small test comfortably within this delay.
                time.sleep(2.0)
                self._progress(60, "Reading on-chip test report", "Reading EEPROM report written by the temporary firmware")
                test_eeprom, report_results, _ = self._read_memory_with_fallback(
                    "eeprom",
                    EEPROM_SIZE,
                    preferred=self._fallback(),
                    progress_callback=self._transfer_progress(60, 67, "Reading self-test report"),
                )
                results.extend(report_results)
                if report_results[-1].ok:
                    report_info = self._parse_selftest_report(test_eeprom)
                else:
                    report_info = {
                        "valid": False,
                        "detail": "The temporary firmware was loaded, but its EEPROM report could not be read.",
                    }
        finally:
            # Always attempt restoration once a complete backup exists, even if
            # the temporary write or report read failed.
            self._progress(70, "Restoring original chip", "Replacing temporary Flash and EEPROM from the recovery backup")
            with tempfile.TemporaryDirectory(prefix="avrx-selftest-restore-") as temp_name:
                temp = Path(temp_name)
                flash_path = temp / "original-flash.hex"
                eeprom_path = temp / "original-eeprom.bin"
                write_intel_hex(bytes_to_memory(original_flash), flash_path)
                eeprom_path.write_bytes(original_eeprom)

                erase_result = self.runner.chip_erase(
                    self._fallback(), timeout=max(90, self._timeout()), log_callback=self._log
                )
                results.append(erase_result)
                if erase_result.ok:
                    flash_restore = self.runner.write_memory(
                        self._fallback(), "flash", flash_path, "i",
                        timeout=self._memory_timeout("flash", "write"),
                        log_callback=self._log,
                        progress_callback=self._transfer_progress(73, 82, "Restoring original Flash"),
                    )
                    results.append(flash_restore)
                    eeprom_restore = self.runner.write_memory(
                        self._fallback(), "eeprom", eeprom_path, "r",
                        timeout=self._memory_timeout("eeprom", "write"),
                        log_callback=self._log,
                        progress_callback=self._transfer_progress(82, 89, "Restoring original EEPROM"),
                    )
                    results.append(eeprom_restore)
                else:
                    flash_restore = erase_result
                    eeprom_restore = erase_result

            if erase_result.ok and flash_restore.ok and eeprom_restore.ok:
                self._progress(90, "Verifying restoration", "Reading Flash and EEPROM back byte-for-byte")
                restored_flash, flash_results, _ = self._read_memory_with_fallback(
                    "flash", FLASH_SIZE, preferred=self._fallback(),
                    progress_callback=self._transfer_progress(90, 94, "Verifying restored Flash"),
                )
                restored_eeprom, eeprom_results, _ = self._read_memory_with_fallback(
                    "eeprom", EEPROM_SIZE, preferred=self._fallback(),
                    progress_callback=self._transfer_progress(94, 97, "Verifying restored EEPROM"),
                )
                results.extend(flash_results + eeprom_results)
                final_state, state_results = self._detect_quiet(self._fallback())
                results.extend(state_results)
                fuses_match = (
                    final_state.lfuse == original_state.lfuse
                    and final_state.hfuse == original_state.hfuse
                    and final_state.efuse == original_state.efuse
                )
                restore_ok = (
                    flash_results[-1].ok
                    and eeprom_results[-1].ok
                    and restored_flash == original_flash
                    and restored_eeprom == original_eeprom
                    and fuses_match
                )
                restore_detail = (
                    "Original Flash and EEPROM were restored byte-for-byte and fuse bytes are unchanged."
                    if restore_ok
                    else "Final readback did not exactly match the recovery backup. Do not program anything else; use the saved backup package for recovery."
                )
            else:
                restore_detail = "The automatic restoration write did not complete. Use the saved recovery backup before any further work."

        report_detail = str(report_info.get("detail", "No report detail."))
        if restore_ok and bool(report_info.get("valid")) and bool(report_info.get("passed")):
            self._progress(100, "On-chip self-test passed", "Original firmware and EEPROM restored and verified")
            return OperationOutcome(
                True,
                "On-chip self-test passed",
                report_detail + "\n\n" + restore_detail + f"\nRecovery backup: {backup}",
                results,
                backup_path=str(backup),
                extra={"selftest": report_info, "restored": True, "test_programmed": test_programmed, "flash_readback": restored_flash, "eeprom_readback": restored_eeprom, "state": final_state},
            )

        self._progress(100, "On-chip self-test needs attention", "See report and restoration status")
        return OperationOutcome(
            False,
            "On-chip self-test did not fully pass",
            report_detail + "\n\n" + restore_detail + f"\nRecovery backup: {backup}",
            results,
            backup_path=str(backup),
            extra={"selftest": report_info, "restored": restore_ok, "test_programmed": test_programmed, "flash_readback": restored_flash, "eeprom_readback": restored_eeprom, "state": final_state},
        )

    def manual(self, argument_text: str) -> OperationOutcome:
        args = shlex.split(argument_text, posix=False)
        self._progress(10, "Running manual AVRDUDE command", "Expert mode command is in progress")
        result = self.runner.run_manual(self._preferred(), args, timeout=max(60, self._timeout()), log_callback=self._log)
        self._progress(100, "Manual command complete" if result.ok else "Manual command failed", result.classification)
        return OperationOutcome(result.ok, "Manual command complete" if result.ok else "Manual command failed", result.output, [result])
