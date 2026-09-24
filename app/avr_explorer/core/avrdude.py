from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .models import Backend, DeviceState, RunResult


SIGNATURE_RE = re.compile(r"Device signature\s*=\s*0x([0-9a-fA-F]+)")
VERSION_RE = re.compile(r"avrdude(?:\.exe)?:\s*Version\s*([^\s,]+)", re.IGNORECASE)
COMPLETION_RE = re.compile(r"avrdude(?:\.exe)?(?::)?\s+done\.\s+Thank you\.", re.IGNORECASE)
TRANSFER_PROGRESS_RE = re.compile(r"\b(Reading|Writing)\s*\|[^|]*\|\s*(\d{1,3})%", re.IGNORECASE)


class AvrDudeError(RuntimeError):
    pass


class AvrDudeRunner:
    def __init__(
        self,
        programmer: str = "usbtiny",
        part: str = "t85",
        bitclock_us: Optional[float] = None,
    ) -> None:
        self.programmer = programmer
        self.part = part
        self.bitclock_us = bitclock_us
        self._suppress_windows_crash_dialogs()

    @staticmethod
    def _suppress_windows_crash_dialogs() -> None:
        if os.name != "nt":
            return
        try:
            # Suppress Windows Error Reporting popups from legacy AVRDUDE builds.
            SEM_FAILCRITICALERRORS = 0x0001
            SEM_NOGPFAULTERRORBOX = 0x0002
            SEM_NOOPENFILEERRORBOX = 0x8000
            ctypes.windll.kernel32.SetErrorMode(
                SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
            )
        except Exception:
            pass

    def base_command(self, backend: Backend) -> List[str]:
        if not backend.exe.exists():
            raise AvrDudeError(f"AVRDUDE executable was not found: {backend.exe}")
        if not backend.conf.exists():
            raise AvrDudeError(f"AVRDUDE configuration was not found: {backend.conf}")
        command = [
            str(backend.exe),
            "-C", str(backend.conf),
            "-p", self.part,
            "-c", self.programmer,
        ]
        if self.bitclock_us is not None:
            value = f"{self.bitclock_us:g}"
            command.extend(["-B", value])
        return command

    def run(
        self,
        backend: Backend,
        extra_args: List[str],
        timeout: int = 45,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[str, int], None]] = None,
    ) -> RunResult:
        command = self.base_command(backend) + list(extra_args)
        if log_callback:
            log_callback("COMMAND: " + subprocess.list2cmdline(command))

        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        start = time.time()
        process = None
        output_bytes = bytearray()
        timed_out = False
        last_progress = None

        def report_segment(segment: str) -> None:
            nonlocal last_progress
            if not progress_callback:
                return
            match = TRANSFER_PROGRESS_RE.search(segment)
            if not match:
                return
            action = match.group(1).title()
            percent = max(0, min(100, int(match.group(2))))
            marker = (action, percent)
            if marker != last_progress:
                last_progress = marker
                progress_callback(action, percent)

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(backend.exe.parent),
                startupinfo=startupinfo,
                creationflags=creationflags,
                universal_newlines=False,
                shell=False,
            )
            chunks = queue.Queue()

            def reader() -> None:
                try:
                    if process is None or process.stdout is None:
                        return
                    while True:
                        chunk = process.stdout.read(1)
                        if not chunk:
                            break
                        chunks.put(chunk)
                finally:
                    chunks.put(None)

            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()
            deadline = start + max(1, int(timeout))
            segment = bytearray()
            reader_done = False

            while True:
                now = time.time()
                if process.poll() is None and now >= deadline:
                    timed_out = True
                    process.kill()

                try:
                    item = chunks.get(timeout=0.10)
                except queue.Empty:
                    item = b""

                if item is None:
                    reader_done = True
                elif item:
                    output_bytes.extend(item)
                    if item in (b"\r", b"\n"):
                        if segment:
                            report_segment(segment.decode("utf-8", errors="replace"))
                            segment.clear()
                    else:
                        segment.extend(item)

                if process.poll() is not None and reader_done and chunks.empty():
                    if segment:
                        report_segment(segment.decode("utf-8", errors="replace"))
                    break

            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader_thread.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
        except OSError as exc:
            output_bytes.extend(str(exc).encode("utf-8", errors="replace"))
            timed_out = False

        duration = time.time() - start
        output = bytes(output_bytes).decode("utf-8", errors="replace")
        if timed_out:
            output += (
                f"\nATtiny85 Explorer: AVRDUDE was stopped after {int(timeout)} seconds "
                "before it reported completion. Memory may be only partially written.\n"
            )
        exit_code = process.returncode if process is not None else -1
        result = self._parse(command, exit_code, output, duration, timed_out)
        if log_callback:
            for line in result.output.replace("\r", "\n").splitlines():
                if line.strip():
                    log_callback(line)
            log_callback(
                f"RESULT: {result.classification}; exit={result.exit_code}; "
                f"duration={result.duration_seconds:.2f}s"
            )
        return result

    @staticmethod
    def _parse(command: List[str], exit_code: int, output: str, duration: float, timed_out: bool) -> RunResult:
        signature_match = SIGNATURE_RE.search(output)
        version_match = VERSION_RE.search(output)
        lower_output = output.lower()
        completed = bool(COMPLETION_RE.search(output))
        verification_error = "verification error" in lower_output or "verification mismatch" in lower_output
        initialization_failed = (
            "initialization failed" in lower_output
            or "unable to open programmer" in lower_output
            or "could not find usb" in lower_output
            or "no usb device" in lower_output
        )

        if timed_out:
            classification = "timeout"
        elif verification_error:
            classification = "verification_error"
        elif exit_code == 0:
            classification = "success"
        elif completed and not verification_error:
            # Some legacy Windows/libusb combinations crash during process cleanup
            # after AVRDUDE has already printed its completion marker.
            classification = "soft_success"
        elif initialization_failed:
            classification = "initialization_failed"
        else:
            classification = "backend_error"

        return RunResult(
            command=command,
            exit_code=exit_code,
            output=output,
            duration_seconds=duration,
            timed_out=timed_out,
            signature=("0x" + signature_match.group(1).upper()) if signature_match else "",
            version=version_match.group(1) if version_match else "",
            completed_marker=completed,
            verification_error=verification_error,
            initialization_failed=initialization_failed,
            classification=classification,
        )

    def read_state(
        self,
        backend: Backend,
        timeout: int = 45,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[DeviceState, RunResult]:
        with tempfile.TemporaryDirectory(prefix="avrx-state-") as temp_name:
            temp = Path(temp_name)
            lfuse_path = temp / "lfuse.bin"
            hfuse_path = temp / "hfuse.bin"
            efuse_path = temp / "efuse.bin"
            lock_path = temp / "lock.bin"
            args = [
                "-v",
                "-U", f"lfuse:r:{lfuse_path}:r",
                "-U", f"hfuse:r:{hfuse_path}:r",
                "-U", f"efuse:r:{efuse_path}:r",
                "-U", f"lock:r:{lock_path}:r",
            ]
            result = self.run(backend, args, timeout=timeout, log_callback=log_callback)

            def read_byte(path: Path) -> Optional[int]:
                try:
                    data = path.read_bytes()
                    return data[0] if data else None
                except OSError:
                    return None

            state = DeviceState(
                signature=result.signature,
                lfuse=read_byte(lfuse_path),
                hfuse=read_byte(hfuse_path),
                efuse=read_byte(efuse_path),
                lock=read_byte(lock_path),
                backend_name=backend.name,
                raw_output=result.output,
            )
            if result.ok and any(value is None for value in (state.lfuse, state.hfuse, state.efuse, state.lock)):
                result.classification = "backend_error"
                result.output += "\nATtiny85 Explorer: one or more state readback files were not created."
            return state, result

    @staticmethod
    def _reports_empty_memory(output: str, memory: str) -> bool:
        lower = output.lower()
        return (
            f"{memory.lower()} is empty" in lower
            and "resulting file has no contents" in lower
        )

    def read_memory(
        self,
        backend: Backend,
        memory: str,
        size: int,
        timeout: int = 45,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[str, int], None]] = None,
    ) -> Tuple[bytes, RunResult]:
        with tempfile.TemporaryDirectory(prefix="avrx-read-") as temp_name:
            output_path = Path(temp_name) / f"{memory}.bin"
            # The Arduino 1.8.19 build of AVRDUDE 6.3 used on Windows 7 does
            # not support the newer -A flag. Read normally and handle AVRDUDE's
            # special completely-blank result below. In that case AVRDUDE prints
            # "Flash is empty, resulting file has no contents" and creates no file.
            args: List[str] = ["-U", f"{memory}:r:{output_path}:r"]
            result = self.run(
                backend,
                args,
                timeout=timeout,
                log_callback=log_callback,
                progress_callback=progress_callback,
            )

            data = b""
            reported_empty = self._reports_empty_memory(result.output, memory)
            if output_path.exists():
                data = output_path.read_bytes()
                if not data and result.ok and reported_empty:
                    data = bytes([0xFF]) * size
                    result.output += (
                        "\nATtiny85 Explorer: AVRDUDE reported blank memory; "
                        "using a full 0xFF readback image."
                    )
            elif result.ok and reported_empty:
                data = bytes([0xFF]) * size
                result.output += (
                    "\nATtiny85 Explorer: AVRDUDE reported blank memory and created no output file; "
                    "using a full 0xFF readback image."
                )
            elif result.ok:
                result.classification = "backend_error"
                result.output += "\nATtiny85 Explorer: memory readback file was not created."

            if len(data) < size:
                data += bytes([0xFF]) * (size - len(data))
            if len(data) > size:
                data = data[:size]
            return data, result

    def write_memory(
        self,
        backend: Backend,
        memory: str,
        file_path: Path,
        file_format: str,
        timeout: int = 60,
        log_callback: Optional[Callable[[str], None]] = None,
        no_erase: bool = False,
        disable_auto_verify: bool = False,
        progress_callback: Optional[Callable[[str, int], None]] = None,
    ) -> RunResult:
        args: List[str] = []
        if no_erase:
            args.append("-D")
        if disable_auto_verify:
            args.append("-V")
        args.extend(["-U", f"{memory}:w:{file_path}:{file_format}"])
        return self.run(
            backend, args, timeout=timeout, log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def verify_memory(
        self,
        backend: Backend,
        memory: str,
        file_path: Path,
        file_format: str,
        timeout: int = 60,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[str, int], None]] = None,
    ) -> RunResult:
        return self.run(
            backend,
            ["-U", f"{memory}:v:{file_path}:{file_format}"],
            timeout=timeout,
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def write_fuse(
        self,
        backend: Backend,
        fuse_name: str,
        value: int,
        timeout: int = 45,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> RunResult:
        if fuse_name not in ("lfuse", "hfuse", "efuse"):
            raise ValueError("Unsupported fuse name")
        return self.run(
            backend,
            ["-u", "-U", f"{fuse_name}:w:0x{value:02X}:m"],
            timeout=timeout,
            log_callback=log_callback,
        )

    def write_lock(
        self,
        backend: Backend,
        value: int,
        timeout: int = 45,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> RunResult:
        return self.run(
            backend,
            ["-u", "-U", f"lock:w:0x{value:02X}:m"],
            timeout=timeout,
            log_callback=log_callback,
        )

    def chip_erase(
        self,
        backend: Backend,
        timeout: int = 45,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> RunResult:
        return self.run(backend, ["-e"], timeout=timeout, log_callback=log_callback)

    def run_manual(
        self,
        backend: Backend,
        extra_args: List[str],
        timeout: int = 60,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> RunResult:
        return self.run(backend, extra_args, timeout=timeout, log_callback=log_callback)
