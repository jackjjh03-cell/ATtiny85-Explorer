from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .fuses import (
    DEVICE_GRADE_LOW_VOLTAGE,
    DEVICE_GRADE_SELECT,
    DEVICE_GRADE_STANDARD,
    SUPPLY_VOLTAGES,
    FuseConfig,
    normalize_device_grade,
    normalize_supply_label,
)
from .models import DeviceState, RunResult


@dataclass
class FailureDiagnosis:
    summary: str
    likely_cause: str
    next_action: str
    severity: str = "warning"

    def format(self) -> str:
        return (
            f"Diagnosis: {self.summary}\n"
            f"Likely cause: {self.likely_cause}\n"
            f"Next step: {self.next_action}"
        )


def diagnose_run_result(result: RunResult, context: str = "") -> FailureDiagnosis:
    output = (result.output or "").lower()
    context_lower = context.lower()

    if result.timed_out or result.classification == "timeout":
        return FailureDiagnosis(
            "The application timeout expired before AVRDUDE reported completion.",
            "The transfer was still running, the selected ISP speed is slow, or the USB/programmer connection stopped responding.",
            "Reread the target before retrying. If the read is stable, retry at Automatic or a faster known-safe ISP setting; do not repeat fuse writes blindly.",
        )

    if "verification error" in output or "verification mismatch" in output or result.verification_error:
        return FailureDiagnosis(
            "The bytes read back did not match the requested data.",
            "An unstable connection, marginal power, overly fast ISP timing, write interruption, or protected memory can cause a mismatch.",
            "Run the connection stability test, reread the affected memory, and compare the first mismatch before writing again.",
        )

    if (
        "could not find usb" in output
        or "no usb device" in output
        or "unable to open programmer" in output
        or "programmer is not responding" in output
    ):
        return FailureDiagnosis(
            "AVRDUDE could not communicate with the USB programmer.",
            "The programmer is disconnected, its driver is unavailable, another process owns it, or the selected backend/programmer type is wrong.",
            "Reconnect the programmer, close other programming software, then test the backend before touching the target chip.",
        )

    if "invalid device signature" in output or "expected signature" in output or "device signature = 0x000000" in output:
        return FailureDiagnosis(
            "The target signature was missing or did not identify an ATtiny85.",
            "Power, ground, RESET, MOSI, MISO, or SCK may be wrong; the ISP clock may be too fast; or a different chip is installed.",
            "Use Detect with Automatic speed, inspect the chip orientation and ISP connections, and confirm the target is powered.",
        )

    if (
        "initialization failed" in output
        or "target doesn't answer" in output
        or "target does not answer" in output
        or result.classification == "initialization_failed"
    ):
        return FailureDiagnosis(
            "The programmer was found, but the target did not enter ISP programming mode.",
            "RESET may not be reaching the chip, the target may lack a usable clock, power/ground may be missing, or ISP timing may be too fast.",
            "Retry Detect at Slow, verify RESET and the three ISP data pins, and consider whether an external-clock fuse was selected without clock hardware.",
        )

    if "locked" in output or "lock bit" in output or "protected" in output:
        return FailureDiagnosis(
            "Memory access appears to be blocked by device protection.",
            "Programmed lock bits can prevent Flash or EEPROM readback and verification.",
            "Read the lock byte. A chip erase is the normal unlock path, but it clears Flash and can erase EEPROM, so preserve a backup whenever readable.",
        )

    if "sck period" in output and ("cannot" in output or "not" in output):
        return FailureDiagnosis(
            "The programmer/backend rejected the selected ISP bit-clock setting.",
            "This programmer or AVRDUDE build may not support adjustable SCK timing in the requested form.",
            "Use Automatic and test another configured AVRDUDE backend; the target data was not necessarily changed.",
        )

    if result.classification == "soft_success":
        return FailureDiagnosis(
            "AVRDUDE printed its normal completion marker but exited abnormally afterward.",
            "Some legacy Windows/libusb combinations fail during process cleanup after the transfer has already completed.",
            "Reread or verify the target. Treat the operation as successful only when the byte-for-byte readback matches.",
            severity="info",
        )

    operation_hint = f" during {context}" if context else ""
    return FailureDiagnosis(
        f"AVRDUDE ended with an unclassified backend error{operation_hint}.",
        "The raw output did not match a known failure pattern.",
        "Use Save diagnostic report, then inspect the final AVRDUDE lines and run the connection stability test before retrying a destructive operation.",
    )


def diagnose_results(results: List[RunResult], context: str = "") -> Optional[FailureDiagnosis]:
    if not results:
        return None
    for result in reversed(results):
        if not result.ok:
            return diagnose_run_result(result, context)
    # Soft success is worth explaining when it is the final result.
    if results[-1].classification == "soft_success":
        return diagnose_run_result(results[-1], context)
    return None


def audit_current_fuses(
    state: DeviceState,
    device_grade: str,
    supply_label: str,
) -> List[str]:
    """Return compact, read-only warnings for the currently programmed fuses."""
    if state.lfuse is None or state.hfuse is None or state.efuse is None:
        return []
    config = FuseConfig.decode(state.lfuse, state.hfuse, state.efuse)
    warnings: List[str] = []
    grade = normalize_device_grade(device_grade)
    supply = normalize_supply_label(supply_label)
    voltage = SUPPLY_VOLTAGES.get(supply)
    frequency = config.nominal_frequency_mhz()

    if grade == DEVICE_GRADE_SELECT or voltage is None:
        warnings.append("Select the chip marking and planned operating voltage to enable the complete speed-grade and BOD audit.")

    if config.reset_disabled:
        warnings.append("RESET is disabled; normal ISP recovery may require high-voltage serial programming.")
    if config.debugwire:
        warnings.append("debugWIRE is enabled and can interfere with ordinary RESET/ISP use until it is disabled correctly.")
    if config.clock_source.startswith("External") or "crystal" in config.clock_source.lower() or "resonator" in config.clock_source.lower():
        warnings.append("The active clock fuse requires external clock hardware; losing that clock can make the target appear unresponsive.")
    if config.clock_source == "Internal 128 kHz":
        warnings.append("The 128 kHz clock may require a slow ISP setting.")

    if voltage is not None:
        thresholds = {"1.8 V": 1.8, "2.7 V": 2.7, "4.3 V": 4.3}
        threshold = thresholds.get(config.bod)
        if threshold is not None and voltage < threshold:
            warnings.append(f"BOD {config.bod} is above planned VCC {supply}; the chip may remain in reset.")
        elif threshold is not None and voltage - threshold < 0.25:
            warnings.append(f"Planned VCC {supply} is close to the BOD {config.bod} threshold.")

    if frequency is not None and grade != DEVICE_GRADE_SELECT and voltage is not None:
        maximum: Optional[float]
        if grade == DEVICE_GRADE_STANDARD:
            maximum = 20.0 if voltage >= 4.5 else (10.0 if voltage >= 2.7 else (4.0 if voltage >= 1.8 else 0.0))
        elif grade == DEVICE_GRADE_LOW_VOLTAGE:
            maximum = 10.0 if voltage >= 2.7 else (4.0 if voltage >= 1.8 else 0.0)
        else:
            maximum = None
        if maximum is not None and frequency > maximum:
            warnings.append(f"{frequency:g} MHz exceeds the selected speed grade's guaranteed {maximum:g} MHz limit at {supply}.")
    if frequency is not None and frequency > 10.0 and config.bod != "4.3 V":
        warnings.append("A high-speed clock is active without 4.3 V BOD; operation during a falling supply may leave the guaranteed speed range.")

    return warnings
