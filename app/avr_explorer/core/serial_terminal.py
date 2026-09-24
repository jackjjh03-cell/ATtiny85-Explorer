from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

# ATtiny85 Explorer carries a local copy of pySerial 3.5 so the source build works
# on an offline Windows 7 computer without pip or an internet connection.
VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor"
if VENDOR_DIR.exists() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception as exc:  # pragma: no cover - exercised only on broken installs
    serial = None
    list_ports = None
    SERIAL_IMPORT_ERROR = str(exc)
else:
    SERIAL_IMPORT_ERROR = ""


@dataclass
class SerialConfig:
    port: str
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    flow_control: str = "None"
    timeout: float = 0.10
    write_timeout: float = 2.0
    dtr: bool = False
    rts: bool = False


@dataclass
class PortInfo:
    device: str
    description: str = ""
    hardware_id: str = ""

    @property
    def display(self) -> str:
        if self.description and self.description != "n/a":
            return f"{self.device} — {self.description}"
        return self.device


def serial_available() -> bool:
    return serial is not None


def serial_import_error() -> str:
    return SERIAL_IMPORT_ERROR


def list_serial_ports() -> List[PortInfo]:
    if list_ports is None:
        return []
    result: List[PortInfo] = []
    try:
        ports = sorted(list_ports.comports(), key=lambda item: item.device.lower())
    except Exception:
        return []
    for item in ports:
        result.append(PortInfo(
            device=str(item.device),
            description=str(getattr(item, "description", "") or ""),
            hardware_id=str(getattr(item, "hwid", "") or ""),
        ))
    return result


def parse_hex_bytes(text: str) -> bytes:
    cleaned = text.replace(",", " ").replace(";", " ").replace("\n", " ").replace("\r", " ")
    tokens = [token for token in cleaned.split() if token]
    if not tokens:
        return b""
    values = bytearray()
    for token in tokens:
        value = token.strip()
        if value.lower().startswith("0x"):
            value = value[2:]
        if len(value) == 0 or len(value) > 2:
            raise ValueError(f"Invalid hex byte: {token!r}")
        try:
            number = int(value, 16)
        except ValueError as exc:
            raise ValueError(f"Invalid hex byte: {token!r}") from exc
        if not 0 <= number <= 0xFF:
            raise ValueError(f"Hex byte outside 00-FF: {token!r}")
        values.append(number)
    return bytes(values)


def format_hex(data: bytes) -> str:
    return " ".join(f"{value:02X}" for value in data)


def format_text_and_hex(data: bytes, encoding: str = "utf-8") -> str:
    text = data.decode(encoding, errors="replace")
    printable = "".join(character if character.isprintable() or character in "\r\n\t" else "." for character in text)
    return f"{format_hex(data):<48} |{printable}|"


class SerialTerminalSession:
    """Threaded serial connection used by the Tk GUI.

    The read loop never touches Tk directly. It emits bytes and status strings
    through callbacks, which the application places on its existing event queue.
    """

    def __init__(
        self,
        on_data: Callable[[bytes], None],
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.on_data = on_data
        self.on_status = on_status
        self.on_error = on_error
        self._serial = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self.config: Optional[SerialConfig] = None

    @property
    def connected(self) -> bool:
        return bool(self._serial is not None and getattr(self._serial, "is_open", False))

    @property
    def port(self) -> str:
        return self.config.port if self.config else ""

    def connect(self, config: SerialConfig) -> None:
        if serial is None:
            raise RuntimeError(f"Serial support is unavailable: {SERIAL_IMPORT_ERROR}")
        self.disconnect()
        if not config.port.strip():
            raise ValueError("Select or enter a serial port.")
        self._stop.clear()
        kwargs = {
            "baudrate": int(config.baudrate),
            "bytesize": int(config.bytesize),
            "parity": str(config.parity),
            "stopbits": float(config.stopbits),
            "timeout": float(config.timeout),
            "write_timeout": float(config.write_timeout),
            "xonxoff": config.flow_control == "XON/XOFF",
            "rtscts": config.flow_control == "RTS/CTS",
            "dsrdtr": config.flow_control == "DSR/DTR",
        }
        # serial_for_url supports normal COM ports plus loop:// for a built-in
        # terminal self-test that requires no hardware.
        connection = serial.serial_for_url(config.port.strip(), **kwargs)
        try:
            connection.dtr = bool(config.dtr)
            connection.rts = bool(config.rts)
            connection.reset_input_buffer()
            connection.reset_output_buffer()
        except Exception:
            connection.close()
            raise
        self._serial = connection
        self.config = config
        self._reader = threading.Thread(target=self._read_loop, name="AVRExplorerSerialReader", daemon=True)
        self._reader.start()
        self.on_status(f"Connected to {config.port} at {config.baudrate} baud")

    def disconnect(self) -> None:
        self._stop.set()
        connection = self._serial
        self._serial = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        reader = self._reader
        self._reader = None
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=0.5)
        if self.config is not None:
            self.on_status("Disconnected")

    def write(self, data: bytes) -> int:
        connection = self._serial
        if connection is None or not getattr(connection, "is_open", False):
            raise RuntimeError("The serial terminal is not connected.")
        if not data:
            return 0
        with self._write_lock:
            count = int(connection.write(data))
            connection.flush()
            return count

    def send_break(self, duration: float = 0.25) -> None:
        connection = self._serial
        if connection is None or not getattr(connection, "is_open", False):
            raise RuntimeError("The serial terminal is not connected.")
        connection.send_break(duration=duration)

    def clear_input(self) -> None:
        if self.connected:
            self._serial.reset_input_buffer()

    def clear_output(self) -> None:
        if self.connected:
            self._serial.reset_output_buffer()

    def set_dtr(self, enabled: bool) -> None:
        if self.connected:
            self._serial.dtr = bool(enabled)

    def set_rts(self, enabled: bool) -> None:
        if self.connected:
            self._serial.rts = bool(enabled)

    def modem_lines(self) -> str:
        if not self.connected:
            return "CTS --   DSR --   RI --   CD --"
        connection = self._serial
        try:
            return (
                f"CTS {'ON' if connection.cts else 'off'}   "
                f"DSR {'ON' if connection.dsr else 'off'}   "
                f"RI {'ON' if connection.ri else 'off'}   "
                f"CD {'ON' if connection.cd else 'off'}"
            )
        except Exception:
            return "Modem-line status unavailable"

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            connection = self._serial
            if connection is None or not getattr(connection, "is_open", False):
                break
            try:
                waiting = int(getattr(connection, "in_waiting", 0) or 0)
                data = connection.read(waiting if waiting > 0 else 1)
                if data:
                    self.on_data(bytes(data))
            except Exception as exc:
                if not self._stop.is_set():
                    self.on_error(str(exc))
                break
            if not data:
                time.sleep(0.01)
        # Only report a lost connection when the read loop ended unexpectedly.
        if not self._stop.is_set() and self._serial is not None:
            self.on_status("Serial connection ended")
