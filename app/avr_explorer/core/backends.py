from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .builtin_backend import (
    BUILTIN_BACKEND_ID,
    BUILTIN_BACKEND_NAME,
    BUILTIN_BACKEND_ROOT,
    verify_builtin_backend,
)
from .models import Backend
from .paths import CONFIG_DIR


class BackendRegistry:
    def __init__(
        self,
        path: Path = CONFIG_DIR / "backends.json",
        built_in_root: Path = BUILTIN_BACKEND_ROOT,
    ) -> None:
        self.path = path
        self.built_in_root = built_in_root
        self.built_in_errors: List[str] = []
        self.backends: List[Backend] = []
        self.load()
        self._ensure_builtin_backend()

    def _ensure_builtin_backend(self) -> None:
        """Register the packaged AVRDUDE and refresh its temporary one-file path."""
        self.built_in_errors = verify_builtin_backend(self.built_in_root)
        existing = self.get(BUILTIN_BACKEND_ID)
        if self.built_in_errors:
            if existing:
                self.backends.remove(existing)
                self.save()
            return

        exe = (self.built_in_root / "bin" / "avrdude.exe").resolve()
        conf = (self.built_in_root / "etc" / "avrdude.conf").resolve()
        if existing is None:
            existing = Backend(
                backend_id=BUILTIN_BACKEND_ID,
                name=BUILTIN_BACKEND_NAME,
                exe_path=str(exe),
                conf_path=str(conf),
                known_good=True,
                preferred=not any(item.preferred and self.paths_exist(item) for item in self.backends),
                fallback=not any(item.fallback and self.paths_exist(item) for item in self.backends),
                enabled=True,
                detected_version="6.3-20190619",
                last_test_status="Built-in files verified",
                built_in=True,
            )
            self.backends.insert(0, existing)
        else:
            existing.name = BUILTIN_BACKEND_NAME
            existing.exe_path = str(exe)
            existing.conf_path = str(conf)
            existing.known_good = True
            existing.enabled = True
            existing.detected_version = existing.detected_version or "6.3-20190619"
            existing.last_test_status = "Built-in files verified"
            existing.built_in = True

        if not any(item.preferred and self.paths_exist(item) for item in self.backends):
            existing.preferred = True
        # Keep the verified 6.3 copy as permanent recovery insurance. A tested
        # external backend may still be selected for normal operations.
        for backend in self.backends:
            backend.fallback = backend.backend_id == BUILTIN_BACKEND_ID
        self.save()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
            self.backends = [Backend.from_dict(item) for item in values if isinstance(item, dict)]
        except (OSError, ValueError, TypeError):
            self.backends = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([backend.to_dict() for backend in self.backends], indent=2),
            encoding="utf-8",
        )

    def add(self, name: str, exe_path: Path, conf_path: Path, known_good: bool = False) -> Backend:
        exe_path = exe_path.resolve()
        conf_path = conf_path.resolve()
        existing = next(
            (backend for backend in self.backends
             if Path(backend.exe_path) == exe_path and Path(backend.conf_path) == conf_path),
            None,
        )
        if existing:
            return existing

        backend = Backend(
            backend_id=uuid.uuid4().hex,
            name=name,
            exe_path=str(exe_path),
            conf_path=str(conf_path),
            known_good=known_good,
        )
        if not self.backends:
            backend.preferred = True
            backend.fallback = True
        elif known_good and not any(item.fallback for item in self.backends):
            backend.fallback = True
        self.backends.append(backend)
        self.save()
        return backend

    def remove(self, backend_id: str) -> None:
        backend = self.get(backend_id)
        if backend and backend.built_in:
            raise ValueError("The built-in AVRDUDE backend is part of ATtiny85 Explorer and cannot be removed.")
        self.backends = [backend for backend in self.backends if backend.backend_id != backend_id]
        if self.backends and not any(item.preferred for item in self.backends):
            self.backends[0].preferred = True
        if self.backends and not any(item.fallback for item in self.backends):
            self.backends[0].fallback = True
        self.save()

    def set_preferred(self, backend_id: str) -> None:
        for backend in self.backends:
            backend.preferred = backend.backend_id == backend_id
        self.save()

    def set_fallback(self, backend_id: str) -> None:
        built_in = self.get(BUILTIN_BACKEND_ID)
        if built_in is not None and backend_id != BUILTIN_BACKEND_ID:
            raise ValueError("The built-in AVRDUDE 6.3 backend is permanently reserved for recovery.")
        for backend in self.backends:
            backend.fallback = backend.backend_id == backend_id
        self.save()

    def preferred(self) -> Optional[Backend]:
        enabled = [backend for backend in self.backends if backend.enabled]
        return next((backend for backend in enabled if backend.preferred), enabled[0] if enabled else None)

    def fallback(self) -> Optional[Backend]:
        enabled = [backend for backend in self.backends if backend.enabled]
        return next((backend for backend in enabled if backend.fallback), self.preferred())

    def get(self, backend_id: str) -> Optional[Backend]:
        return next((backend for backend in self.backends if backend.backend_id == backend_id), None)

    def update_test(self, backend_id: str, status: str, version: str = "") -> None:
        backend = self.get(backend_id)
        if backend:
            backend.last_test_status = status
            backend.detected_version = version or backend.detected_version
            backend.last_tested_at = datetime.now().isoformat(timespec="seconds")
            self.save()

    @staticmethod
    def paths_exist(backend: Backend) -> bool:
        """Return True when a backend still has its executable and config file."""
        return backend.exe.is_file() and backend.conf.is_file()

    @staticmethod
    def _matching_conf(exe: Path) -> Optional[Path]:
        """Locate the config file belonging to one AVRDUDE executable."""
        candidates = [
            exe.parent.parent / "etc" / "avrdude.conf",
            exe.parent / "avrdude.conf",
            exe.parent.parent / "avrdude.conf",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def find_in_directory(cls, directory: Path, max_depth: int = 8) -> List[Tuple[Path, Path]]:
        """Find self-contained AVRDUDE backends below an Arduino directory.

        The selected location may be the Arduino IDE root, ``hardware/tools/avr``,
        an Arduino15 packages directory, or the backend's ``bin`` directory.
        Search depth is limited so an accidentally broad selection does not scan
        an entire drive indefinitely on older Windows systems.
        """
        directory = directory.expanduser()
        if directory.is_file():
            directory = directory.parent
        try:
            directory = directory.resolve()
        except OSError:
            return []
        if not directory.is_dir():
            return []

        executables: List[Path] = []
        direct_candidates = [
            directory / "avrdude.exe",
            directory / "bin" / "avrdude.exe",
            directory / "hardware" / "tools" / "avr" / "bin" / "avrdude.exe",
        ]
        if directory.name.lower() in ("bin", "etc"):
            direct_candidates.append(directory.parent / "bin" / "avrdude.exe")

        for candidate in direct_candidates:
            if candidate.is_file():
                executables.append(candidate)

        try:
            for current_root, child_dirs, file_names in os.walk(str(directory)):
                current = Path(current_root)
                try:
                    depth = len(current.relative_to(directory).parts)
                except ValueError:
                    depth = max_depth + 1
                if depth >= max_depth:
                    child_dirs[:] = []
                lower_names = {name.lower(): name for name in file_names}
                if "avrdude.exe" in lower_names:
                    executables.append(current / lower_names["avrdude.exe"])
        except OSError:
            pass

        found: List[Tuple[Path, Path]] = []
        seen = set()
        for exe in executables:
            conf = cls._matching_conf(exe)
            if conf is None:
                continue
            try:
                exe = exe.resolve()
                conf = conf.resolve()
            except OSError:
                continue
            key = (str(exe).lower(), str(conf).lower())
            if key not in seen:
                seen.add(key)
                found.append((exe, conf))
        return found

    @staticmethod
    def _looks_known_good(path: Path) -> bool:
        text = str(path).lower()
        return "arduino-1.8.19" in text or "6.3-20190619" in text

    @staticmethod
    def _display_name(exe: Path, known_good: bool) -> str:
        if known_good:
            return "AVRDUDE 6.3 from Arduino 1.8.19"
        bundle_root = exe.parent.parent
        return f"AVRDUDE at {bundle_root}"

    def add_from_directory(self, directory: Path) -> Tuple[List[Backend], List[Backend]]:
        """Register backends from a user-selected Arduino directory.

        Missing registrations are repaired in place where possible so their
        preferred/fallback roles survive when the Arduino folder is moved.
        Returns ``(added, updated)``.
        """
        matches = self.find_in_directory(directory)
        added: List[Backend] = []
        updated: List[Backend] = []
        stale = [backend for backend in self.backends if not self.paths_exist(backend)]

        for exe, conf in matches:
            existing = next(
                (
                    backend for backend in self.backends
                    if str(backend.exe).lower() == str(exe).lower()
                    and str(backend.conf).lower() == str(conf).lower()
                ),
                None,
            )
            if existing:
                continue

            known_good = self._looks_known_good(exe)
            repair: Optional[Backend] = None
            if len(matches) == 1 and len(stale) == 1:
                repair = stale[0]
            if repair is None:
                repair = next(
                    (
                        backend for backend in stale
                        if backend.known_good == known_good
                        and (backend.preferred or backend.fallback)
                    ),
                    None,
                )
            if repair is None and len(matches) == 1:
                repair = next(
                    (backend for backend in stale if backend.preferred or backend.fallback),
                    None,
                )

            if repair is not None:
                repair.exe_path = str(exe)
                repair.conf_path = str(conf)
                repair.known_good = repair.known_good or known_good
                if known_good:
                    repair.name = self._display_name(exe, True)
                repair.detected_version = ""
                repair.last_test_status = "Path updated - not tested"
                repair.last_tested_at = ""
                updated.append(repair)
                stale.remove(repair)
            else:
                backend = self.add(
                    self._display_name(exe, known_good),
                    exe,
                    conf,
                    known_good=known_good,
                )
                added.append(backend)

        usable = updated + added
        if usable:
            preferred = next((backend for backend in self.backends if backend.preferred), None)
            fallback = next((backend for backend in self.backends if backend.fallback), None)
            if preferred is None or not self.paths_exist(preferred):
                for backend in self.backends:
                    backend.preferred = backend is usable[0]
            if fallback is None or not self.paths_exist(fallback):
                fallback_choice = next((item for item in usable if item.known_good), usable[0])
                for backend in self.backends:
                    backend.fallback = backend is fallback_choice
            self.save()
        elif updated:
            self.save()
        return added, updated

    @staticmethod
    def _candidate_roots() -> Iterable[Path]:
        user_profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        local_appdata = Path(os.environ.get("LOCALAPPDATA", user_profile / "AppData" / "Local"))
        program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))

        direct = [
            user_profile / "Desktop" / "arduino-1.8.19" / "hardware" / "tools" / "avr",
            user_profile / "Desktop" / "arduino-1.8.18" / "hardware" / "tools" / "avr",
            user_profile / "OneDrive" / "Desktop" / "arduino-1.8.19" / "hardware" / "tools" / "avr",
            user_profile / "OneDrive" / "Desktop" / "arduino-1.8.18" / "hardware" / "tools" / "avr",
            program_files_x86 / "Arduino" / "hardware" / "tools" / "avr",
            program_files / "Arduino" / "hardware" / "tools" / "avr",
        ]
        for path in direct:
            yield path

        arduino15 = local_appdata / "Arduino15" / "packages"
        if arduino15.exists():
            try:
                for exe in arduino15.glob("**/tools/avrdude/*/bin/avrdude.exe"):
                    yield exe.parent.parent
            except OSError:
                pass

    def auto_discover(self) -> List[Backend]:
        added: List[Backend] = []
        seen = set()
        for root in self._candidate_roots():
            exe = root / "bin" / "avrdude.exe"
            conf = root / "etc" / "avrdude.conf"
            key = (str(exe).lower(), str(conf).lower())
            if key in seen:
                continue
            seen.add(key)
            if exe.exists() and conf.exists():
                known_good = self._looks_known_good(root)
                name = self._display_name(exe, known_good)
                added.append(self.add(name, exe, conf, known_good=known_good))
        return added
