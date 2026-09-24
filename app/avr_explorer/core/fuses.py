from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import RiskItem


# Clock-source definitions come from the ATtiny25/45/85 CKSEL tables.  Additional
# fields are deliberately human-facing: the fuse bits only choose an oscillator
# mode; they do not prove that the required voltage or external hardware exists.
CLOCK_SOURCES: Dict[str, Dict[str, object]] = {
    "Internal RC 8 MHz": {
        "cksel": 0x2,
        "sut": ("00", "01", "10"),
        "default_sut": "10",
        "risk": "Low",
        "base_mhz": 8.0,
        "description": "Calibrated internal 8 MHz RC oscillator; no external clock parts are required.",
        "hardware": "None. PB3 and PB4 remain available as GPIO unless another peripheral uses them.",
        "pins": "No dedicated clock pins are consumed.",
        "accuracy": "Factory calibrated, but less accurate than a crystal and affected by voltage and temperature.",
        "failure": "Firmware compiled for the wrong F_CPU will have incorrect delays, timers, PWM and serial baud rates.",
        "recovery": "Normal ISP remains available because this source is internal.",
    },
    "High-frequency PLL 16 MHz": {
        "cksel": 0x1,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "Medium",
        "base_mhz": 16.0,
        "description": "Internal PLL-derived 16 MHz system clock. The internal 64 MHz PLL is divided by four for the CPU clock.",
        "hardware": "No external crystal is required. Use a stable regulated supply.",
        "pins": "No dedicated clock pins are consumed.",
        "accuracy": "The PLL is locked to the internal RC oscillator, so it is not crystal-accurate.",
        "failure": "At insufficient voltage the CPU is outside its guaranteed speed grade. Wrong F_CPU causes timing and serial errors.",
        "recovery": "Normal ISP remains available because this source is internal. Reprogram the low fuse to return to 1 or 8 MHz.",
    },
    "ATtiny15 compatibility (1.6 MHz system)": {
        "cksel": 0x3,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "00",
        "risk": "Medium",
        "base_mhz": 1.6,
        "description": "ATtiny15 compatibility mode: a 6.4 MHz calibrated source is divided by four, producing a 1.6 MHz system clock.",
        "hardware": "None, but this mode changes compatibility behavior and is intended for legacy ATtiny15 designs.",
        "pins": "ATtiny15 compatibility changes some peripheral and pin behavior; consult the datasheet before use.",
        "accuracy": "Internal calibrated source; not crystal-accurate.",
        "failure": "Modern firmware rarely expects this mode. Timers, pin mappings and F_CPU assumptions can be wrong.",
        "recovery": "Normal ISP remains available because the source is internal.",
    },
    "Internal 128 kHz": {
        "cksel": 0x4,
        "sut": ("00", "01", "10"),
        "default_sut": "10",
        "risk": "Low",
        "base_mhz": 0.128,
        "description": "Low-power internal 128 kHz oscillator.",
        "hardware": "None.",
        "pins": "No dedicated clock pins are consumed.",
        "accuracy": "Nominal frequency; intended for low power rather than precision timing.",
        "failure": "ISP may require a slow programming clock. Firmware compiled for a faster F_CPU will appear extremely slow.",
        "recovery": "Use a programmer capable of slow ISP clocking, then restore an internal 1 or 8 MHz preset.",
    },
    "External clock on PB3/CLKI": {
        "cksel": 0x0,
        "sut": ("00", "01", "10"),
        "default_sut": "10",
        "risk": "High",
        "base_mhz": None,
        "description": "Requires a continuous external logic-level clock signal on PB3/CLKI.",
        "hardware": "A clock generator or oscillator module must already drive PB3/CLKI before this fuse is programmed.",
        "pins": "PB3 is consumed by CLKI. PB4 remains available unless CKOUT or another peripheral uses it.",
        "accuracy": "Determined entirely by the external clock source.",
        "failure": "Without the clock signal the chip does not execute normally and may appear dead to ISP.",
        "recovery": "Restore a suitable clock signal on PB3/CLKI, then program an internal-clock preset. HVSP is another recovery path.",
    },
    "32.768 kHz low-frequency crystal": {
        "cksel": 0x6,
        "sut": ("00", "01", "10"),
        "default_sut": "10",
        "risk": "High",
        "base_mhz": 0.032768,
        "description": "Requires a 32.768 kHz watch crystal connected to PB3/XTAL1 and PB4/XTAL2.",
        "hardware": "32.768 kHz crystal and the load arrangement required by the crystal/datasheet.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Crystal accuracy; slow start-up is normal.",
        "failure": "Missing or incorrectly loaded crystal prevents normal execution and can make ISP appear unavailable.",
        "recovery": "Fit the crystal or provide a suitable temporary clock to PB3/XTAL1, then restore an internal-clock preset.",
    },
    "Crystal/resonator 0.4-0.9 MHz, CKSEL0=0": {
        "cksel": 0x8,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "External ceramic resonator mode for approximately 0.4 to 0.9 MHz.",
        "hardware": "Ceramic resonator on PB3/PB4 with capacitor values specified by its manufacturer.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the resonator.",
        "failure": "Missing or unsuitable resonator prevents normal execution.",
        "recovery": "Reconnect the resonator or inject a suitable clock at PB3/XTAL1, then restore an internal preset.",
    },
    "Crystal/resonator 0.4-0.9 MHz, CKSEL0=1": {
        "cksel": 0x9,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "External crystal/resonator mode for approximately 0.4 to 0.9 MHz.",
        "hardware": "Crystal or resonator on PB3/PB4. Crystal capacitor values must match its load capacitance.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the external component.",
        "failure": "Missing or unsuitable clock hardware prevents normal execution.",
        "recovery": "Reconnect the oscillator hardware or inject a suitable clock at PB3/XTAL1, then restore an internal preset.",
    },
    "Crystal/resonator 0.9-3.0 MHz, CKSEL0=0": {
        "cksel": 0xA,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "External ceramic resonator mode for approximately 0.9 to 3.0 MHz.",
        "hardware": "Ceramic resonator on PB3/PB4 with manufacturer-specified capacitors.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the resonator.",
        "failure": "Missing or unsuitable resonator prevents normal execution.",
        "recovery": "Reconnect the resonator or inject a suitable clock at PB3/XTAL1, then restore an internal preset.",
    },
    "Crystal/resonator 0.9-3.0 MHz, CKSEL0=1": {
        "cksel": 0xB,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "External crystal/resonator mode for approximately 0.9 to 3.0 MHz.",
        "hardware": "Crystal or resonator on PB3/PB4. The datasheet gives 12-22 pF as a starting range for crystals in this band.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the external component.",
        "failure": "Missing or unsuitable clock hardware prevents normal execution.",
        "recovery": "Reconnect the oscillator hardware or inject a suitable clock at PB3/XTAL1, then restore an internal preset.",
    },
    "Crystal/resonator 3.0-8.0 MHz, CKSEL0=0": {
        "cksel": 0xC,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "External ceramic resonator mode for approximately 3.0 to 8.0 MHz.",
        "hardware": "Ceramic resonator on PB3/PB4 with manufacturer-specified capacitors.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the resonator.",
        "failure": "Missing or unsuitable resonator prevents normal execution.",
        "recovery": "Reconnect the resonator or inject a suitable clock at PB3/XTAL1, then restore an internal preset.",
    },
    "Crystal/resonator 3.0-8.0 MHz, CKSEL0=1": {
        "cksel": 0xD,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "External crystal/resonator mode for approximately 3.0 to 8.0 MHz.",
        "hardware": "Crystal or resonator on PB3/PB4. The datasheet gives 12-22 pF as a starting range for crystals in this band.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the external component.",
        "failure": "Missing or unsuitable clock hardware prevents normal execution.",
        "recovery": "Reconnect the oscillator hardware or inject a suitable clock at PB3/XTAL1, then restore an internal preset.",
    },
    "Crystal/resonator 8.0 MHz and above, CKSEL0=0": {
        "cksel": 0xE,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "High-frequency ceramic resonator mode. Observe the device voltage/frequency speed grade.",
        "hardware": "Ceramic resonator on PB3/PB4 with manufacturer-specified capacitors.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the resonator.",
        "failure": "Missing hardware or an excessive clock/voltage combination can prevent reliable operation.",
        "recovery": "Reconnect the resonator or inject a suitable clock at PB3/XTAL1, then restore an internal preset.",
    },
    "Crystal/resonator 8.0 MHz and above, CKSEL0=1": {
        "cksel": 0xF,
        "sut": ("00", "01", "10", "11"),
        "default_sut": "11",
        "risk": "High",
        "base_mhz": None,
        "description": "High-frequency crystal/resonator mode. Observe the device voltage/frequency speed grade.",
        "hardware": "Crystal or resonator on PB3/PB4. For crystals, 12-22 pF is a datasheet starting range; calculate the actual values from crystal load capacitance and stray capacitance.",
        "pins": "PB3 and PB4 are consumed by the oscillator.",
        "accuracy": "Determined by the external crystal or resonator.",
        "failure": "Missing hardware or an excessive clock/voltage combination can prevent reliable operation and ISP access.",
        "recovery": "Reconnect the oscillator hardware or inject a suitable clock at PB3/XTAL1, then restore an internal preset. HVSP is another recovery path.",
    },
}

BOD_CODES = {
    "Disabled": 0b111,
    "1.8 V": 0b110,
    "2.7 V": 0b101,
    "4.3 V": 0b100,
}
BOD_NAMES = {value: key for key, value in BOD_CODES.items()}

STARTUP_DESCRIPTIONS = {
    "00": "Shortest start-up. Recommended for many sources when BOD is enabled.",
    "01": "Fast-rising power delay.",
    "10": "Slowly-rising power / long standard delay.",
    "11": "Source-specific. For the PLL and high-frequency crystal presets this is the longest start-up choice.",
}

DEVICE_GRADE_SELECT = "Select chip marking"
DEVICE_GRADE_STANDARD = "20U / ATtiny85-20PU (standard, up to 20 MHz)"
DEVICE_GRADE_LOW_VOLTAGE = "ATtiny85V-10 (low-voltage, up to 10 MHz)"
SUPPLY_SELECT = "Select planned operating voltage"

DEVICE_GRADES: Dict[str, Dict[str, object]] = {
    DEVICE_GRADE_SELECT: {
        "description": "The electronic signature cannot identify the speed-grade suffix. Read the text printed on the package.",
    },
    DEVICE_GRADE_STANDARD: {
        "description": "Use this for common DIP parts marked 20U or ordered as ATtiny85-20PU. Rated up to 10 MHz at 2.7-4.49 V and up to 20 MHz at 4.5-5.5 V.",
    },
    DEVICE_GRADE_LOW_VOLTAGE: {
        "description": "Low-voltage V-suffix device. Up to 4 MHz below 2.7 V and up to 10 MHz from 2.7-5.5 V. Do not use 16 or 20 MHz.",
    },
}

# The selected value is the voltage planned for the finished circuit. ATtiny85 Explorer
# cannot electrically measure or change VCC. High-speed profiles separately require
# a meter-verification acknowledgement before they can be programmed.
SUPPLY_VOLTAGES: Dict[str, Optional[float]] = {
    SUPPLY_SELECT: None,
    "1.8 V": 1.8,
    "2.0 V": 2.0,
    "2.7 V": 2.7,
    "3.0 V": 3.0,
    "3.3 V": 3.3,
    "4.5 V": 4.5,
    "5.0 V": 5.0,
    "5.5 V": 5.5,
}

LEGACY_DEVICE_GRADE_ALIASES = {
    "ATtiny85-20 (standard, up to 20 MHz)": DEVICE_GRADE_STANDARD,
}
LEGACY_SUPPLY_ALIASES = {
    "Select measured supply": SUPPLY_SELECT,
}


def normalize_device_grade(value: str) -> str:
    return LEGACY_DEVICE_GRADE_ALIASES.get(value, value)


def normalize_supply_label(value: str) -> str:
    return LEGACY_SUPPLY_ALIASES.get(value, value)


@dataclass
class FuseConfig:
    clock_source: str = "Internal RC 8 MHz"
    sut: str = "10"
    divide_by_8: bool = True
    clock_output: bool = False
    watchdog_always_on: bool = False
    preserve_eeprom: bool = False
    bod: str = "Disabled"
    debugwire: bool = False
    reset_disabled: bool = False
    self_programming: bool = False

    def encode(self) -> Tuple[int, int, int]:
        if self.clock_source not in CLOCK_SOURCES:
            raise ValueError("Unknown clock source")
        source = CLOCK_SOURCES[self.clock_source]
        if self.sut not in source["sut"]:
            raise ValueError("The selected start-up time is invalid for this clock source.")
        if self.bod not in BOD_CODES:
            raise ValueError("Unknown brown-out setting")

        cksel = int(source["cksel"])
        sut_value = int(self.sut, 2)

        lfuse = 0
        lfuse |= (0 if self.divide_by_8 else 1) << 7
        lfuse |= (0 if self.clock_output else 1) << 6
        lfuse |= (sut_value & 0x3) << 4
        lfuse |= cksel & 0xF

        hfuse = 0
        hfuse |= (0 if self.reset_disabled else 1) << 7
        hfuse |= (0 if self.debugwire else 1) << 6
        hfuse |= 0 << 5
        hfuse |= (0 if self.watchdog_always_on else 1) << 4
        hfuse |= (0 if self.preserve_eeprom else 1) << 3
        hfuse |= BOD_CODES[self.bod] & 0x7

        efuse = 0xFE | (0 if self.self_programming else 1)
        return lfuse & 0xFF, hfuse & 0xFF, efuse & 0xFF

    @classmethod
    def decode(cls, lfuse: int, hfuse: int, efuse: int) -> "FuseConfig":
        cksel = lfuse & 0xF
        source_name = next(
            (name for name, item in CLOCK_SOURCES.items() if int(item["cksel"]) == cksel),
            "Internal RC 8 MHz",
        )
        sut = f"{(lfuse >> 4) & 0x3:02b}"
        bod_code = hfuse & 0x7
        return cls(
            clock_source=source_name,
            sut=sut,
            divide_by_8=((lfuse >> 7) & 1) == 0,
            clock_output=((lfuse >> 6) & 1) == 0,
            watchdog_always_on=((hfuse >> 4) & 1) == 0,
            preserve_eeprom=((hfuse >> 3) & 1) == 0,
            bod=BOD_NAMES.get(bod_code, f"Reserved code {bod_code:03b}"),
            debugwire=((hfuse >> 6) & 1) == 0,
            reset_disabled=((hfuse >> 7) & 1) == 0,
            self_programming=(efuse & 1) == 0,
        )

    def nominal_frequency_mhz(self, preset_name: str = "") -> Optional[float]:
        preset = PRESET_INFO.get(preset_name, {})
        preset_frequency = preset.get("frequency_mhz")
        if isinstance(preset_frequency, (int, float)):
            return float(preset_frequency)
        source = CLOCK_SOURCES[self.clock_source]
        base = source.get("base_mhz")
        if not isinstance(base, (int, float)):
            return None
        if self.clock_source == "ATtiny15 compatibility (1.6 MHz system)":
            return 1.6
        return float(base) / (8.0 if self.divide_by_8 else 1.0)

    def system_clock_description(self, preset_name: str = "") -> str:
        frequency = self.nominal_frequency_mhz(preset_name)
        divider = "divided by 8" if self.divide_by_8 else "not divided by 8"
        if frequency is not None:
            if frequency < 0.1:
                frequency_text = f"{frequency * 1000:g} kHz"
            else:
                frequency_text = f"{frequency:g} MHz"
            return f"{frequency_text} system clock ({self.clock_source}, {divider})"
        return f"{self.clock_source}; actual frequency is set by the connected hardware; {divider}"

    def risk_items(self, raw_override: bool = False) -> List[RiskItem]:
        risks: List[RiskItem] = []
        source = CLOCK_SOURCES[self.clock_source]
        if source["risk"] == "High":
            risks.append(RiskItem(
                "High",
                "External clock hardware required",
                str(source["description"]) + " If the hardware is absent or wrong, the chip may stop running and normal ISP can appear unavailable.",
                "CLOCK HARDWARE VERIFIED",
            ))
        if self.clock_source == "High-frequency PLL 16 MHz" and not self.divide_by_8:
            risks.append(RiskItem(
                "High",
                "16 MHz voltage and device grade must be verified",
                "16 MHz is guaranteed only on the standard ATtiny85-20 speed grade at 4.5-5.5 V. The ATtiny85V-10 is not rated for 16 MHz.",
                "16 MHZ SUPPLY VERIFIED",
            ))
        if self.reset_disabled:
            risks.append(RiskItem(
                "Critical",
                "RESET pin will be disabled",
                "PB5 becomes GPIO and normal ISP programming will stop working. Recovery requires high-voltage serial programming.",
                "I UNDERSTAND ISP MAY BE DISABLED",
            ))
        if self.debugwire:
            risks.append(RiskItem(
                "High",
                "debugWIRE will be enabled",
                "debugWIRE can interfere with normal ISP and must be disabled when lock-bit security is required.",
                "ENABLE DEBUGWIRE",
            ))
        if self.watchdog_always_on:
            risks.append(RiskItem("Medium", "Watchdog always on", "Firmware must service the watchdog immediately after reset."))
        if self.clock_output:
            risks.append(RiskItem("Medium", "PB4 becomes clock output", "Normal PB4 GPIO operation is overridden."))
        if self.self_programming:
            risks.append(RiskItem("Medium", "Self-programming enabled", "SPM instructions may modify flash when firmware uses them."))
        if raw_override:
            risks.append(RiskItem(
                "Critical",
                "Raw fuse override",
                "Raw bytes bypass the guided fuse editor. Reserved or inaccessible combinations may make the chip unusable over ISP.",
                "WRITE RAW FUSES",
            ))
        return risks


@dataclass
class FuseChangePlan:
    current_values: Optional[Tuple[int, int, int]]
    target_values: Tuple[int, int, int]
    changed_fuses: List[str]
    setting_changes: List[str]

    @property
    def current_known(self) -> bool:
        return self.current_values is not None

    @property
    def requires_programming(self) -> bool:
        return bool(self.changed_fuses)

    @property
    def active(self) -> bool:
        return self.current_values == self.target_values

    def formatted_lines(self) -> List[str]:
        names = ("LFUSE", "HFUSE", "EFUSE")
        lines: List[str] = []
        if self.current_values is None:
            lines.append("Current fuse bytes have not been read. Read chip fuses before programming.")
            lines.append(
                "Target: " + "   ".join(
                    f"{name}=0x{value:02X}" for name, value in zip(names, self.target_values)
                )
            )
            return lines
        for name, current, target in zip(names, self.current_values, self.target_values):
            if current == target:
                lines.append(f"{name}: 0x{current:02X} (no change)")
            else:
                lines.append(f"{name}: 0x{current:02X} -> 0x{target:02X}  WILL BE WRITTEN")
        if self.setting_changes:
            lines.append("")
            lines.append("Decoded setting changes:")
            lines.extend("- " + item for item in self.setting_changes)
        return lines


def fuse_change_plan(
    current_values: Optional[Tuple[int, int, int]],
    target_values: Tuple[int, int, int],
    target_preset_name: str = "",
) -> FuseChangePlan:
    names = ("LFUSE", "HFUSE", "EFUSE")
    changed = [] if current_values is None else [
        name for name, current, target in zip(names, current_values, target_values) if current != target
    ]
    setting_changes: List[str] = []
    if current_values is not None:
        current = FuseConfig.decode(*current_values)
        target = FuseConfig.decode(*target_values)
        comparisons = [
            ("Clock source", current.clock_source, target.clock_source),
            ("Start-up bits", current.sut, target.sut),
            ("Divide clock by 8 (CKDIV8)", "enabled" if current.divide_by_8 else "disabled", "enabled" if target.divide_by_8 else "disabled"),
            ("Clock output on PB4 (CKOUT)", "enabled" if current.clock_output else "disabled", "enabled" if target.clock_output else "disabled"),
            ("Brown-out detection", current.bod, target.bod),
            ("Preserve EEPROM on chip erase (EESAVE)", "enabled" if current.preserve_eeprom else "disabled", "enabled" if target.preserve_eeprom else "disabled"),
            ("Watchdog always on (WDTON)", "enabled" if current.watchdog_always_on else "disabled", "enabled" if target.watchdog_always_on else "disabled"),
            ("debugWIRE (DWEN)", "enabled" if current.debugwire else "disabled", "enabled" if target.debugwire else "disabled"),
            ("RESET pin disabled (RSTDISBL)", "yes" if current.reset_disabled else "no", "yes" if target.reset_disabled else "no"),
            ("Flash self-programming (SELFPRGEN)", "enabled" if current.self_programming else "disabled", "enabled" if target.self_programming else "disabled"),
        ]
        for label, before, after in comparisons:
            if before != after:
                setting_changes.append(f"{label}: {before} -> {after}")
        current_clock = current.system_clock_description()
        target_clock = target.system_clock_description(target_preset_name)
        if current_clock != target_clock:
            setting_changes.insert(0, f"Resulting system clock: {current_clock} -> {target_clock}")
    return FuseChangePlan(current_values, target_values, changed, setting_changes)


# Values are complete, guided setups. External 16 MHz and 20 MHz crystal
# presets intentionally encode the same fuse bytes; the physical crystal sets
# the actual frequency.
SAFE_PRESETS: Dict[str, FuseConfig] = {
    "Factory default: Internal 1 MHz": FuseConfig(),
    "Internal 1 MHz, preserve EEPROM": FuseConfig(preserve_eeprom=True),
    "Internal 8 MHz, preserve EEPROM": FuseConfig(divide_by_8=False, preserve_eeprom=True),
    "Internal 8 MHz, BOD 2.7 V, preserve EEPROM": FuseConfig(divide_by_8=False, preserve_eeprom=True, bod="2.7 V"),
    "Internal PLL 16 MHz, 5 V, BOD 4.3 V, preserve EEPROM": FuseConfig(
        clock_source="High-frequency PLL 16 MHz",
        sut="00",
        divide_by_8=False,
        preserve_eeprom=True,
        bod="4.3 V",
    ),
    "Internal 128 kHz, BOD disabled": FuseConfig(clock_source="Internal 128 kHz", divide_by_8=False, preserve_eeprom=True),
    "External 10 MHz crystal, BOD 2.7 V, preserve EEPROM": FuseConfig(
        clock_source="Crystal/resonator 8.0 MHz and above, CKSEL0=1",
        sut="11",
        divide_by_8=False,
        preserve_eeprom=True,
        bod="2.7 V",
    ),
    "External 16 MHz crystal, 5 V, BOD 4.3 V, preserve EEPROM": FuseConfig(
        clock_source="Crystal/resonator 8.0 MHz and above, CKSEL0=1",
        sut="11",
        divide_by_8=False,
        preserve_eeprom=True,
        bod="4.3 V",
    ),
    "External 20 MHz crystal, 5 V, BOD 4.3 V, preserve EEPROM": FuseConfig(
        clock_source="Crystal/resonator 8.0 MHz and above, CKSEL0=1",
        sut="11",
        divide_by_8=False,
        preserve_eeprom=True,
        bod="4.3 V",
    ),
    "External 20 MHz clock on PB3, 5 V, BOD 4.3 V, preserve EEPROM": FuseConfig(
        clock_source="External clock on PB3/CLKI",
        sut="00",
        divide_by_8=False,
        preserve_eeprom=True,
        bod="4.3 V",
    ),
}

PRESET_INFO: Dict[str, Dict[str, object]] = {
    "Factory default: Internal 1 MHz": {
        "frequency_mhz": 1.0,
        "f_cpu": "1000000UL",
        "supply": "Standard ATtiny85: 2.7-5.5 V. ATtiny85V: 1.8-5.5 V.",
        "hardware": "No external clock hardware.",
        "pins": "No clock pins consumed.",
        "why": "Conservative factory configuration and easiest ISP recovery setting.",
    },
    "Internal 1 MHz, preserve EEPROM": {
        "frequency_mhz": 1.0,
        "f_cpu": "1000000UL",
        "supply": "Standard ATtiny85: 2.7-5.5 V. ATtiny85V: 1.8-5.5 V.",
        "hardware": "No external clock hardware.",
        "pins": "No clock pins consumed.",
        "why": "Low speed with EEPROM retained through chip erase.",
    },
    "Internal 8 MHz, preserve EEPROM": {
        "frequency_mhz": 8.0,
        "f_cpu": "8000000UL",
        "supply": "2.7-5.5 V for either speed grade.",
        "hardware": "No external clock hardware.",
        "pins": "No clock pins consumed.",
        "why": "Fastest simple internal-RC setting that remains valid at 3.3 V.",
    },
    "Internal 8 MHz, BOD 2.7 V, preserve EEPROM": {
        "frequency_mhz": 8.0,
        "f_cpu": "8000000UL",
        "supply": "Use a stable 3.3 V or 5 V supply. BOD resets the chip near 2.7 V.",
        "hardware": "No external clock hardware.",
        "pins": "No clock pins consumed.",
        "why": "Good general-purpose regulated 3.3 V or 5 V setup with brown-out protection.",
    },
    "Internal PLL 16 MHz, 5 V, BOD 4.3 V, preserve EEPROM": {
        "frequency_mhz": 16.0,
        "f_cpu": "16000000UL",
        "supply": "Required: standard 20U / ATtiny85-20PU at a planned 4.5-5.5 V, verified with a meter under normal load. Recommended: regulated 5.0 V. Not valid for ATtiny85V-10.",
        "hardware": "No crystal is needed. Use a stable regulated supply and local 0.1 uF decoupling from VCC to GND.",
        "pins": "No clock pins consumed.",
        "why": "Highest internal system-clock option. BOD 4.3 V and SUT=00 are selected for a regulated 5 V supply.",
        "warning": "BOD 4.3 V is useful protection but is not permission to operate below the 4.5 V speed-grade limit.",
    },
    "Internal 128 kHz, BOD disabled": {
        "frequency_mhz": 0.128,
        "f_cpu": "128000UL",
        "supply": "Within the selected device grade; intended for low power.",
        "hardware": "No external clock hardware.",
        "pins": "No clock pins consumed.",
        "why": "Very low power and slow execution. A slow ISP clock may be required.",
    },
    "External 10 MHz crystal, BOD 2.7 V, preserve EEPROM": {
        "frequency_mhz": 10.0,
        "f_cpu": "10000000UL",
        "supply": "2.7-5.5 V for either speed grade.",
        "hardware": "10 MHz crystal between PB3/XTAL1 and PB4/XTAL2 plus two load capacitors. 12-22 pF is a starting range; calculate from the crystal data sheet.",
        "pins": "PB3 and PB4 are unavailable as normal GPIO.",
        "why": "Crystal-accurate clock while remaining within the 10 MHz rating at 3.3 V.",
        "warning": "The chip may appear dead if the crystal or capacitors are absent or incorrect.",
    },
    "External 16 MHz crystal, 5 V, BOD 4.3 V, preserve EEPROM": {
        "frequency_mhz": 16.0,
        "f_cpu": "16000000UL",
        "supply": "Required: standard 20U / ATtiny85-20PU at a planned 4.5-5.5 V, verified with a meter under normal load. Not valid for ATtiny85V-10.",
        "hardware": "16 MHz crystal between PB3/XTAL1 and PB4/XTAL2 plus two load capacitors. 12-22 pF is a starting range; calculate from the crystal data sheet.",
        "pins": "PB3 and PB4 are unavailable as normal GPIO.",
        "why": "More accurate than the internal PLL for serial, timing and measurement work.",
        "warning": "Program this only after the crystal network is physically connected and verified.",
    },
    "External 20 MHz crystal, 5 V, BOD 4.3 V, preserve EEPROM": {
        "frequency_mhz": 20.0,
        "f_cpu": "20000000UL",
        "supply": "Required: standard 20U / ATtiny85-20PU at a planned 4.5-5.5 V, verified with a meter under normal load. Not valid for ATtiny85V-10.",
        "hardware": "20 MHz crystal between PB3/XTAL1 and PB4/XTAL2 plus two load capacitors. 12-22 pF is a starting range; calculate from the crystal data sheet.",
        "pins": "PB3 and PB4 are unavailable as normal GPIO.",
        "why": "Maximum rated system frequency for the standard ATtiny85-20.",
        "warning": "This operates at the device limit. Use a clean regulated 5 V rail, short oscillator traces and correct capacitors.",
    },
    "External 20 MHz clock on PB3, 5 V, BOD 4.3 V, preserve EEPROM": {
        "frequency_mhz": 20.0,
        "f_cpu": "20000000UL",
        "supply": "Required: standard 20U / ATtiny85-20PU at a planned 4.5-5.5 V, verified with a meter under normal load. The clock signal logic levels must match VCC.",
        "hardware": "A continuous 20 MHz logic clock must drive PB3/CLKI before the fuse is changed. Do not connect a crystal for this mode.",
        "pins": "PB3 is unavailable as normal GPIO; PB4 remains available.",
        "why": "Use when another oscillator module or controller supplies the system clock.",
        "warning": "Removing the clock source stops the CPU. Avoid abrupt clock-frequency changes while the chip is running.",
    },
}


@dataclass
class ClockSetupReport:
    status: str
    frequency_mhz: Optional[float]
    errors: List[str]
    warnings: List[str]
    requirements: List[str]
    confirmation_phrase: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.errors)


def _max_frequency_for_environment(device_grade: str, voltage: float) -> Optional[float]:
    device_grade = normalize_device_grade(device_grade)
    if device_grade == DEVICE_GRADE_STANDARD:
        if voltage < 2.7 or voltage > 5.5:
            return 0.0
        return 20.0 if voltage >= 4.5 else 10.0
    if device_grade == DEVICE_GRADE_LOW_VOLTAGE:
        if voltage < 1.8 or voltage > 5.5:
            return 0.0
        return 10.0 if voltage >= 2.7 else 4.0
    return None


def clock_setup_report(
    config: FuseConfig,
    device_grade: str,
    supply_label: str,
    preset_name: str = "",
    voltage_verified: bool = False,
    clock_hardware_ready: bool = False,
    firmware_clock_acknowledged: bool = False,
    external_frequency_mhz: Optional[float] = None,
) -> ClockSetupReport:
    device_grade = normalize_device_grade(device_grade)
    supply_label = normalize_supply_label(supply_label)
    source = CLOCK_SOURCES[config.clock_source]
    preset = PRESET_INFO.get(preset_name, {})
    frequency = config.nominal_frequency_mhz(preset_name)
    external = source.get("risk") == "High"
    if external and frequency is None and external_frequency_mhz is not None:
        frequency = external_frequency_mhz
    voltage = SUPPLY_VOLTAGES.get(supply_label)
    errors: List[str] = []
    warnings: List[str] = []
    requirements: List[str] = []

    requirements.append("Activation: this clock choice is a fuse configuration. It is not active until the required fuse bytes are programmed and read back successfully.")
    requirements.append("Voltage input: the planned operating voltage is a safety-check value; ATtiny85 Explorer does not generate, regulate, or measure VCC.")
    clock_description = config.system_clock_description(preset_name)
    if external and config.nominal_frequency_mhz(preset_name) is None and frequency is not None:
        clock_description += f"; entered external frequency: {frequency:g} MHz"
    requirements.append("Clock source: " + clock_description)
    requirements.append("Hardware: " + str(preset.get("hardware", source.get("hardware", "See datasheet."))))
    requirements.append("Pins: " + str(preset.get("pins", source.get("pins", "See datasheet."))))
    requirements.append("Supply: " + str(preset.get("supply", "Verify the selected device speed grade and supply voltage.")))
    if preset.get("f_cpu"):
        requirements.append("Compile firmware with F_CPU=" + str(preset["f_cpu"]) + ".")
    elif frequency is not None:
        f_cpu_value = int(round(frequency * 1000000.0))
        requirements.append(f"Compile firmware with F_CPU={f_cpu_value}UL for the entered/derived system frequency.")
    else:
        requirements.append("The fuse cannot encode the exact external frequency. Enter the connected hardware frequency before programming.")
    if preset.get("why"):
        requirements.append("Purpose: " + str(preset["why"]))
    requirements.append("Accuracy: " + str(source.get("accuracy", "See datasheet.")))
    requirements.append("If wrong: " + str(preset.get("warning", source.get("failure", "The chip may not run as expected."))))
    requirements.append("Recovery: " + str(source.get("recovery", "Restore a known-good clock, then reprogram the fuses.")))

    high_speed = frequency is not None and frequency > 10.0

    guided_external = bool(preset_name and preset_name.startswith("External"))
    if device_grade == DEVICE_GRADE_SELECT:
        errors.append("Select the chip marking before programming fuses. Use the 20U / ATtiny85-20PU choice for a common DIP chip marked 20U.")
    if voltage is None:
        errors.append("Select the planned operating voltage before programming fuses so speed-grade and BOD safety can be checked.")
    if external and config.nominal_frequency_mhz(preset_name) is None and external_frequency_mhz is None:
        errors.append("Enter the actual external clock/crystal frequency in MHz, or use a guided external preset. The fuse bits alone do not encode the exact frequency.")

    if high_speed and not voltage_verified:
        errors.append("Verify VCC at the chip with a meter under normal load and check the voltage-verification box. This is required above 10 MHz.")
    if external and not clock_hardware_ready:
        errors.append("Confirm that the required external crystal, resonator, or clock source is installed and operating before programming the clock fuse.")
    if (high_speed or external) and not firmware_clock_acknowledged:
        errors.append("Confirm that the firmware is or will be compiled for the displayed F_CPU. Wrong F_CPU breaks delays, timers, PWM and serial timing.")

    if voltage is not None and device_grade != DEVICE_GRADE_SELECT:
        maximum = _max_frequency_for_environment(device_grade, voltage)
        if maximum == 0.0:
            errors.append(f"Planned VCC {supply_label} is outside the operating range for {device_grade}.")
        elif frequency is not None and maximum is not None and frequency > maximum + 1e-9:
            errors.append(
                f"{frequency:g} MHz exceeds the guaranteed {maximum:g} MHz limit for {device_grade} at planned VCC {supply_label}."
            )

    bod_thresholds = {"1.8 V": 1.8, "2.7 V": 2.7, "4.3 V": 4.3}
    bod_threshold = bod_thresholds.get(config.bod)
    if voltage is not None and bod_threshold is not None:
        if voltage < bod_threshold:
            errors.append(
                f"BOD {config.bod} is above planned VCC {supply_label}, so the chip will normally remain in reset."
            )
        elif voltage - bod_threshold < 0.25:
            warnings.append(
                f"Planned VCC is close to the BOD {config.bod} threshold; tolerance and ripple may cause resets."
            )

    if config.clock_source == "High-frequency PLL 16 MHz" and not config.divide_by_8:
        if config.bod != "4.3 V":
            warnings.append("For the guided 16 MHz/5 V setup, BOD 4.3 V is recommended.")
        if config.sut != "00" and config.bod == "4.3 V":
            warnings.append("With BOD enabled, SUT=00 is the datasheet-recommended PLL start-up choice.")
    if config.clock_source == "Internal 128 kHz":
        warnings.append("A slow ISP clock may be required after switching to 128 kHz.")
    if external:
        warnings.append("External clock hardware must be installed and running before the fuse is programmed.")
    if external and config.nominal_frequency_mhz(preset_name) is None and frequency is not None:
        warnings.append("Manual external-source mode uses the entered frequency only for safety checks and F_CPU guidance; the connected hardware sets the real frequency.")

    phrase = ""
    if external and high_speed:
        phrase = "CLOCK HARDWARE AND 5V VERIFIED"
    elif external:
        phrase = "CLOCK HARDWARE VERIFIED"
    elif high_speed:
        phrase = "16 MHZ SUPPLY VERIFIED"

    status = "BLOCKED" if errors else ("CHECK WARNINGS" if warnings else "READY")
    return ClockSetupReport(status, frequency, errors, warnings, requirements, phrase)


def decode_lock(lock_value: int) -> str:
    mode = lock_value & 0x3
    if mode == 0x3:
        return "Mode 1: Unlocked"
    if mode == 0x2:
        return "Mode 2: Further flash/EEPROM programming disabled; fuses locked"
    if mode == 0x0:
        return "Mode 3: Further programming and verification disabled; fuses locked"
    return "Reserved lock-bit combination"


def validate_raw_fuses(lfuse: int, hfuse: int, efuse: int) -> List[str]:
    errors: List[str] = []
    if not all(0 <= value <= 0xFF for value in (lfuse, hfuse, efuse)):
        errors.append("Fuse values must be between 0x00 and 0xFF.")
    if (efuse & 0xFE) != 0xFE:
        errors.append("ATtiny85 extended-fuse bits 7:1 are reserved and must remain 1.")
    if ((hfuse >> 5) & 1) != 0:
        errors.append("SPIEN cannot be unprogrammed through this SPI programmer. Keep HFUSE bit 5 at 0.")
    if (hfuse & 0x7) not in BOD_NAMES:
        errors.append("BODLEVEL uses a reserved encoding.")
    if (lfuse & 0xF) in (0x5, 0x7):
        errors.append("CKSEL code is reserved for ATtiny85.")
    return errors
