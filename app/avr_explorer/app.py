from __future__ import annotations

import json
import logging
import os
import platform
import queue
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .core.backends import BackendRegistry
from .core.diagnostics import audit_current_fuses, diagnose_results
from .core.backups import read_backup_package, validate_backup_package
from .core.fuses import (
    BOD_CODES,
    CLOCK_SOURCES,
    DEVICE_GRADES,
    DEVICE_GRADE_SELECT,
    DEVICE_GRADE_STANDARD,
    SAFE_PRESETS,
    STARTUP_DESCRIPTIONS,
    SUPPLY_SELECT,
    SUPPLY_VOLTAGES,
    FuseConfig,
    clock_setup_report,
    decode_lock,
    fuse_change_plan,
    normalize_device_grade,
    normalize_supply_label,
    validate_raw_fuses,
)
from .core.intelhex import bytes_to_memory, inspect_image, load_image, write_intel_hex
from .core.models import Backend, DeviceState, OperationOutcome, RiskItem
from .core.operations import EEPROM_PAGE_SIZE, EEPROM_SIZE, FLASH_PAGE_SIZE, FLASH_SIZE, OperationService
from .core.paths import BACKUP_DIR, LOG_DIR, PROJECT_DIR
from .core.memory_tools import (
    MemoryStats,
    analyze_memory,
    editor_text_from_memory,
    memory_from_editor_text,
)
from .core.settings import SettingsStore
from .core.projects import create_project_package, read_project_package
from .core.serial_terminal import (
    SerialConfig,
    SerialTerminalSession,
    format_hex,
    format_text_and_hex,
    list_serial_ports,
    parse_hex_bytes,
    serial_available,
    serial_import_error,
)


APP_TITLE = f"ATtiny85 Explorer v{__version__}"


def parse_number(text: str) -> int:
    value = text.strip().lower()
    if not value:
        return 0
    return int(value, 0)


def format_byte(value: Optional[int]) -> str:
    return "--" if value is None else f"0x{value:02X}"


def format_bytes(value: int) -> str:
    count = int(value)
    return f"{count:,} byte" if count == 1 else f"{count:,} bytes"


def format_bits(value: int) -> str:
    count = int(value)
    return f"{count:,} bit" if count == 1 else f"{count:,} bits"


def format_capacity(value: int) -> str:
    count = int(value)
    return f"{format_bytes(count)} ({format_bits(count * 8)})"


def format_pages(used: int, total: int, page_size: int) -> str:
    return f"{used:,} / {total:,} pages ({format_bytes(page_size)} per page)"


class ConfirmDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        title: str,
        summary: str,
        phrase: str = "",
        button_text: str = "Continue",
    ) -> None:
        super().__init__(master)
        self.result = False
        self.phrase = phrase
        self.required_response = "YES" if phrase else ""
        self.title(title)
        self.transient(master)
        self.resizable(True, True)
        self.minsize(650, 430)
        self.grab_set()

        frame = ttk.Frame(self, padding=14)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text=title, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        message_frame = ttk.Frame(frame)
        message_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 10))
        message = tk.Text(message_frame, width=78, height=17, wrap="word", relief="flat", background=self.cget("background"))
        message_scroll = ttk.Scrollbar(message_frame, orient="vertical", command=message.yview)
        message.configure(yscrollcommand=message_scroll.set)
        message.grid(row=0, column=0, sticky="nsew")
        message_scroll.grid(row=0, column=1, sticky="ns")
        message_frame.columnconfigure(0, weight=1)
        message_frame.rowconfigure(0, weight=1)
        message.insert("1.0", summary)
        message.configure(state="disabled")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        self.entry_var = tk.StringVar()
        if phrase:
            protected_action = phrase if phrase.upper() != "YES" else title
            ttk.Label(
                frame,
                text=f"Protected action: {protected_action}",
                foreground="#9b1c1c",
            ).grid(row=2, column=0, columnspan=2, sticky="w")
            ttk.Label(frame, text="Type YES to confirm:").grid(row=3, column=0, sticky="w", pady=(4, 0))
            entry = ttk.Entry(frame, textvariable=self.entry_var, width=18)
            entry.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
            entry.focus_set()
            entry.select_range(0, "end")
            self.entry_var.trace_add("write", self._update_button)

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=(0, 8))
        self.confirm_button = ttk.Button(buttons, text=button_text, command=self._confirm)
        self.confirm_button.grid(row=0, column=1)
        if phrase:
            self.confirm_button.configure(state="disabled")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.bind("<Return>", lambda _event: self._confirm() if str(self.confirm_button["state"]) != "disabled" else None)
        self.update_idletasks()
        self.geometry(f"+{master.winfo_rootx() + 80}+{master.winfo_rooty() + 60}")

    def _typed_confirmation_matches(self) -> bool:
        return self.entry_var.get().strip().upper() == self.required_response

    def _update_button(self, *_args) -> None:
        self.confirm_button.configure(state="normal" if self._typed_confirmation_matches() else "disabled")

    def _confirm(self) -> None:
        if self.phrase and not self._typed_confirmation_matches():
            return
        self.result = True
        self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()


class AVRExplorerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1120x690")
        self.minsize(900, 580)
        self.option_add("*tearOff", False)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Danger.TLabel", foreground="#8B0000")
        style.configure("Memory.Horizontal.TProgressbar", thickness=18)

        self.settings = SettingsStore()
        saved_geometry = str(self.settings.get("window_geometry", "1120x690")).strip()
        if saved_geometry:
            try:
                self.geometry(saved_geometry)
            except tk.TclError:
                self.geometry("1120x690")
        self.registry = BackendRegistry()
        saved_arduino_directory = str(self.settings.get("arduino_directory", "")).strip()
        if saved_arduino_directory:
            self.registry.add_from_directory(Path(saved_arduino_directory))
        self.event_queue: "queue.Queue[Tuple]" = queue.Queue()
        self.service = OperationService(
            self.registry,
            self.settings,
            log_callback=self._thread_log,
            progress_callback=self._thread_progress,
        )
        self.busy = False
        self.current_state = DeviceState()
        self.memory_images: Dict[str, bytes] = {}
        self.memory_original_images: Dict[str, bytes] = {}
        self.editor_images: Dict[str, bytearray] = {}
        self.editor_baselines: Dict[str, bytes] = {}
        self.editor_sources: Dict[str, str] = {}
        self.editor_loaded_paths: Dict[str, str] = {"flash": "", "eeprom": ""}
        self.editor_dirty: Dict[str, bool] = {"flash": False, "eeprom": False}
        self.editor_undo: Dict[str, List[bytes]] = {"flash": [], "eeprom": []}
        self.editor_redo: Dict[str, List[bytes]] = {"flash": [], "eeprom": []}
        self.retry_write_ready: Dict[str, bool] = {"flash": False, "eeprom": False}
        self.memory_statistics: Dict[str, MemoryStats] = {}
        self.loaded_project: Optional[Dict[str, object]] = None
        self.loaded_project_path: Optional[Path] = None
        self.selected_backup_path: Optional[Path] = None
        self.log_history: List[str] = []
        self.last_avrdude_command = ""
        self.last_failure_diagnosis = ""
        self._memory_layout_initialized = {"flash": False, "eeprom": False}
        self.terminal_send_history: List[str] = []
        self.terminal_history_index = 0
        self.serial_session = SerialTerminalSession(
            on_data=lambda data: self.event_queue.put(("terminal_data", data)),
            on_status=lambda status: self.event_queue.put(("terminal_status", status)),
            on_error=lambda error: self.event_queue.put(("terminal_error", error)),
        )
        self.terminal_rx_bytes = 0
        self.terminal_tx_bytes = 0
        self.terminal_capture = bytearray()
        self.terminal_port_map: Dict[str, str] = {}
        self.log_path = LOG_DIR / f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        self.logger = logging.getLogger("avr_explorer")
        self.logger.setLevel(logging.INFO)
        handler = logging.FileHandler(str(self.log_path), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.logger.addHandler(handler)

        self.mode_var = tk.StringVar(value=str(self.settings.get("mode", "Basic")))
        self.status_var = tk.StringVar(value="Ready")
        self.backend_var = tk.StringVar(value="Not configured")
        self.target_var = tk.StringVar(value="Not detected")
        self.signature_var = tk.StringVar(value="--")
        self.clock_status_var = tk.StringVar(value="--")
        self.fuse_status_var = tk.StringVar(value="--")
        self.lock_status_var = tk.StringVar(value="--")
        self.progress_value_var = tk.DoubleVar(value=0.0)
        self.progress_percent_var = tk.StringVar(value="Idle")
        self.progress_detail_var = tk.StringVar(value="No operation running")

        self.operation_backup_var = tk.BooleanVar(value=bool(self.settings.get("automatic_backup", True)))
        self.operation_verify_var = tk.BooleanVar(value=bool(self.settings.get("verify_after_write", True)))
        self.operation_full_readback_var = tk.BooleanVar(value=bool(self.settings.get("full_readback_verification", False)))
        self.isp_speed_var = tk.StringVar(value=str(self.settings.get("isp_speed_mode", "Automatic")))
        self.operation_options_summary_var = tk.StringVar(value="")

        self._build_menu()
        self._build_header()
        self._build_notebook()
        self._apply_mode()
        self._refresh_backend_tree()
        self.after(100, self._poll_events)
        self.after(250, self._initial_backend_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- application plumbing ----------

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu)
        file_menu.add_command(label="Projects", command=lambda: self.notebook.select(self.projects_tab))
        file_menu.add_command(label="Backup manager", command=lambda: self.notebook.select(self.backups_tab))
        file_menu.add_separator()
        file_menu.add_command(label="Create complete backup", command=self._backup_now)
        file_menu.add_command(label="Restore backup package", command=self._restore_backup)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menu)
        tools_menu.add_command(label="Detect ATtiny85", command=self._detect)
        tools_menu.add_command(label="Add backend folder...", command=self._select_arduino_directory)
        tools_menu.add_command(label="Auto-discover AVRDUDE", command=self._auto_discover_backends)
        tools_menu.add_command(label="Open data folder", command=self._open_data_folder)
        tools_menu.add_separator()
        tools_menu.add_command(label="Open device terminal", command=lambda: self.notebook.select(self.terminal_tab))
        menu.add_cascade(label="Tools", menu=tools_menu)

        view_menu = tk.Menu(menu)
        view_menu.add_command(label="Reset memory split", command=self._reset_panel_layout)
        menu.add_cascade(label="View", menu=view_menu)
        self.configure(menu=menu)

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(10, 8))
        header.pack(fill="x")
        ttk.Label(header, text=f"ATtiny85 Explorer v{__version__}", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="ATtiny85 / USBtinyISP", foreground="#555555").grid(row=1, column=0, sticky="w")

        mode_frame = ttk.Frame(header)
        mode_frame.grid(row=0, column=1, rowspan=2, padx=(24, 10), sticky="w")
        ttk.Label(mode_frame, text="Mode:").grid(row=0, column=0, sticky="w")
        mode_box = ttk.Combobox(
            mode_frame,
            textvariable=self.mode_var,
            state="readonly",
            values=("Basic", "Advanced", "Expert"),
            width=10,
        )
        mode_box.grid(row=1, column=0, sticky="w", pady=(2, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self._mode_changed())

        progress_box = ttk.Frame(header)
        progress_box.grid(row=0, column=2, rowspan=2, padx=(8, 14), sticky="ew")
        progress_box.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            progress_box,
            mode="determinate",
            maximum=100,
            length=260,
            variable=self.progress_value_var,
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_box, textvariable=self.progress_percent_var, width=6, anchor="e").grid(row=0, column=1, padx=(6, 0))
        ttk.Label(progress_box, textvariable=self.status_var, style="Status.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            progress_box,
            textvariable=self.progress_detail_var,
            foreground="#555555",
            width=28,
            anchor="e",
        ).grid(row=1, column=1, sticky="e", padx=(6, 0), pady=(2, 0))

        programmer = ttk.LabelFrame(header, text="Programmer", padding=(7, 4))
        programmer.grid(row=0, column=3, rowspan=2, sticky="e")
        ttk.Label(programmer, text="ISP speed").grid(row=0, column=0, sticky="w")
        self.header_isp_speed_combo = ttk.Combobox(
            programmer,
            textvariable=self.isp_speed_var,
            state="readonly",
            width=16,
            values=(
                "Automatic",
                "Fast (1 µs)",
                "Compatible (10 µs)",
                "Slow (40 µs)",
            ),
        )
        self.header_isp_speed_combo.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.header_isp_speed_combo.bind("<<ComboboxSelected>>", lambda _event: self._save_operation_options())
        ttk.Button(programmer, text="?", width=3, command=self._show_isp_speed_help).grid(row=1, column=1, padx=(4, 0))
        ttk.Button(programmer, text="Detect", command=self._detect).grid(row=0, column=2, rowspan=2, padx=(9, 0), sticky="ns")

        header.columnconfigure(2, weight=1)

        status = ttk.Frame(self, padding=(10, 2, 10, 8))
        status.pack(fill="x")
        items = [
            ("Backend", self.backend_var),
            ("Target", self.target_var),
            ("Signature", self.signature_var),
            ("Clock", self.clock_status_var),
            ("Fuses", self.fuse_status_var),
            ("Lock", self.lock_status_var),
        ]
        for column, (label, variable) in enumerate(items):
            box = ttk.LabelFrame(status, text=label, padding=(8, 4))
            box.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))
            ttk.Label(box, textvariable=variable).pack(anchor="w")
            status.columnconfigure(column, weight=1)

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.dashboard_tab = ttk.Frame(self.notebook, padding=10)
        self.projects_tab = ttk.Frame(self.notebook, padding=10)
        self.backups_tab = ttk.Frame(self.notebook, padding=10)
        self.flash_tab = ttk.Frame(self.notebook, padding=10)
        self.eeprom_tab = ttk.Frame(self.notebook, padding=10)
        self.fuses_tab = ttk.Frame(self.notebook, padding=10)
        self.lock_tab = ttk.Frame(self.notebook, padding=10)
        self.backends_tab = ttk.Frame(self.notebook, padding=10)
        self.terminal_tab = ttk.Frame(self.notebook, padding=8)
        self.logs_tab = ttk.Frame(self.notebook, padding=8)
        self.info_tab = ttk.Frame(self.notebook, padding=10)
        for tab, title in [
            (self.dashboard_tab, "Dashboard"),
            (self.projects_tab, "Projects"),
            (self.backups_tab, "Backups"),
            (self.flash_tab, "Flash"),
            (self.eeprom_tab, "EEPROM"),
            (self.fuses_tab, "Fuses"),
            (self.lock_tab, "Lock Bits"),
            (self.backends_tab, "Backends"),
            (self.terminal_tab, "Device Terminal"),
            (self.logs_tab, "Logs"),
            (self.info_tab, "Information"),
        ]:
            self.notebook.add(tab, text=title)
        self.notebook.bind("<<NotebookTabChanged>>", self._memory_tab_became_visible, add="+")

        self._build_dashboard_tab()
        self._build_projects_tab()
        self._build_backups_tab()
        self._build_flash_tab()
        self._build_eeprom_tab()
        self._build_fuses_tab()
        self._build_lock_tab()
        self._build_backends_tab()
        self._build_terminal_tab()
        self._build_logs_tab()
        self._build_info_tab()

    def _run_task(self, description: str, function: Callable[[], object], callback: Optional[Callable[[object], None]] = None) -> None:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "Another operation is already running.", parent=self)
            return
        self.busy = True
        self.status_var.set(description)
        self.progress_value_var.set(0)
        self.progress_percent_var.set("0%")
        self.progress_detail_var.set("Starting")

        def worker() -> None:
            try:
                result = function()
                self.event_queue.put(("done", description, result, callback))
            except Exception as exc:
                self.event_queue.put(("error", description, exc, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    def _thread_log(self, text: str) -> None:
        self.event_queue.put(("log", text))

    def _thread_progress(self, percent: int, stage: str, detail: str = "") -> None:
        self.event_queue.put(("progress", percent, stage, detail))

    def _set_operation_progress(self, percent: int, stage: str, detail: str = "") -> None:
        value = max(0, min(100, int(percent)))
        self.progress_value_var.set(value)
        self.progress_percent_var.set(f"{value}%")
        self.status_var.set(stage)
        self.progress_detail_var.set(detail or "Working")

    def _reset_progress_if_idle(self) -> None:
        if self.busy:
            return
        self.progress_value_var.set(0)
        self.progress_percent_var.set("Idle")
        self.progress_detail_var.set("No operation running")
        if self.status_var.get() in ("Complete", "Ready") or self.status_var.get().endswith("complete"):
            self.status_var.set("Ready")

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "log":
                self._append_log(event[1])
            elif kind == "progress":
                _kind, percent, stage, detail = event
                self._set_operation_progress(percent, stage, detail)
            elif kind == "terminal_data":
                self._terminal_receive(event[1])
            elif kind == "terminal_status":
                self._terminal_set_status(event[1])
            elif kind == "terminal_error":
                self._terminal_connection_error(event[1])
            elif kind == "done":
                _kind, description, result, callback = event
                self.busy = False
                if self.progress_value_var.get() < 100:
                    self._set_operation_progress(100, "Complete", description)
                if callback:
                    callback(result)
                elif isinstance(result, OperationOutcome):
                    self._show_outcome(result)
                self.after(1800, self._reset_progress_if_idle)
            elif kind == "error":
                _kind, description, exc, trace = event
                self.busy = False
                self.progress_value_var.set(100)
                self.progress_percent_var.set("Error")
                self.status_var.set("Error")
                self.progress_detail_var.set(description)
                self._append_log(trace)
                messagebox.showerror(APP_TITLE, f"{description} failed:\n\n{exc}", parent=self)
        self.after(100, self._poll_events)

    def _append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        self.logger.info(text)
        self.log_history.append(line)
        if len(self.log_history) > 5000:
            self.log_history = self.log_history[-5000:]
        if text.startswith("COMMAND:"):
            self.last_avrdude_command = text[len("COMMAND:"):].strip()
        if hasattr(self, "log_text") and (self.log_show_details_var.get() or self._is_summary_log(text)):
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > 1200:
                self.log_text.delete("1.0", f"{line_count - 1000}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    @staticmethod
    def _is_summary_log(text: str) -> bool:
        lowered = text.lower()
        return (
            text.startswith(("COMMAND:", "RESULT:", "Automatic backup", "Read failed", "Fuse write", "Serial terminal"))
            or "verified" in lowered
            or "failed" in lowered
            or "error" in lowered
            or "backup" in lowered
            or "signature" in lowered
            or "retry" in lowered
        )

    def _show_outcome(self, outcome: OperationOutcome) -> None:
        detail = outcome.detail
        diagnosis = diagnose_results(outcome.run_results, outcome.title)
        if not outcome.success and diagnosis is not None:
            diagnosis_text = diagnosis.format()
            self.last_failure_diagnosis = diagnosis_text
            if "Diagnosis:" not in detail:
                detail += "\n\n" + diagnosis_text
        self._append_log(f"{outcome.title}: {detail}")
        if outcome.success:
            messagebox.showinfo(outcome.title, detail, parent=self)
        else:
            messagebox.showerror(outcome.title, detail, parent=self)

    def _confirm(self, title: str, summary: str, phrase: str = "", button_text: str = "Continue") -> bool:
        dialog = ConfirmDialog(self, title, summary, phrase=phrase, button_text=button_text)
        self.wait_window(dialog)
        return dialog.result

    def _mode_changed(self) -> None:
        self.settings.set("mode", self.mode_var.get())
        self._apply_mode()

    def _apply_mode(self) -> None:
        mode = self.mode_var.get()
        advanced = mode in ("Advanced", "Expert")
        expert = mode == "Expert"

        for attribute in ("flash_advanced_frame", "eeprom_advanced_frame"):
            if hasattr(self, attribute):
                widget = getattr(self, attribute)
                if advanced:
                    widget.grid()
                else:
                    widget.grid_remove()

        if hasattr(self, "fuse_advanced_frame"):
            if advanced:
                self.fuse_advanced_frame.grid()
            else:
                self.fuse_advanced_frame.grid_remove()
        if hasattr(self, "raw_fuse_frame"):
            if expert:
                self.raw_fuse_frame.grid()
            else:
                self.raw_fuse_frame.grid_remove()

        if hasattr(self, "manual_frame"):
            if expert:
                self.manual_frame.grid()
            else:
                self.manual_frame.grid_remove()
        if hasattr(self, "clock_combo"):
            safe_values = ["Internal RC 8 MHz", "High-frequency PLL 16 MHz", "Internal 128 kHz"]
            self.clock_combo.configure(values=tuple(CLOCK_SOURCES.keys()) if advanced else tuple(safe_values))
            if not advanced and self.clock_var.get() not in safe_values:
                self.clock_var.set("Internal RC 8 MHz")
        if hasattr(self, "preset_combo"):
            basic_presets = tuple(name for name in SAFE_PRESETS if not name.startswith("External"))
            available_presets = tuple(SAFE_PRESETS.keys()) if advanced else basic_presets
            self.preset_combo.configure(values=available_presets)
            if self.preset_var.get() not in available_presets:
                self.preset_var.set(available_presets[0])

        # Lock programming and chip erase are deliberately Expert-only. The tab
        # remains readable in every mode, with an explicit reminder instead of
        # controls that merely look broken.
        if hasattr(self, "lock_mode1_radio"):
            state = "normal" if expert else "disabled"
            self.lock_mode1_radio.configure(state=state)
            self.lock_mode2_radio.configure(state=state)
            self.lock_mode3_radio.configure(state=state)
            self.lock_erase_button.configure(state=state)
            self.lock_program_button.configure(state=state)
            if expert:
                self.lock_mode_notice_frame.grid_remove()
            else:
                self.lock_mode_notice_frame.grid()

        if hasattr(self, "proposed_fuse_label"):
            self._refresh_fuse_preview()

    def _initial_backend_status(self) -> None:
        backend = self.registry.preferred()
        self.backend_var.set(backend.name if backend else "No backend configured")

    def _open_data_folder(self) -> None:
        from .core.paths import DATA_DIR
        try:
            import os
            os.startfile(str(DATA_DIR))
        except Exception:
            messagebox.showinfo(APP_TITLE, str(DATA_DIR), parent=self)

    # ---------- dashboard ----------

    def _build_dashboard_tab(self) -> None:
        left = ttk.LabelFrame(self.dashboard_tab, text="Connection and health", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.LabelFrame(self.dashboard_tab, text="Quick tools", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        self.dashboard_tab.columnconfigure(0, weight=2)
        self.dashboard_tab.columnconfigure(1, weight=1)
        self.dashboard_tab.rowconfigure(0, weight=1)

        self.dashboard_text = tk.Text(left, height=17, wrap="word", relief="flat")
        self.dashboard_text.pack(fill="both", expand=True)
        self.dashboard_text.insert(
            "1.0",
            "Connect the FabISP/USBtiny programmer and click Detect.\n\n"
            "Detection and connection testing are read-only.",
        )
        self.dashboard_text.configure(state="disabled")

        primary = (
            ("Detect ATtiny85", self._detect),
            ("Read full chip → workspace", self._read_full_chip_to_workspace),
            ("Test connection stability", self._connection_stability_test),
            ("Run on-chip self-test…", self._run_on_chip_self_test),
            ("Create complete backup", self._backup_now),
        )
        for row, (text, command) in enumerate(primary):
            ttk.Button(right, text=text, command=command).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Separator(right).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 7))
        ttk.Button(right, text="Projects", command=lambda: self.notebook.select(self.projects_tab)).grid(row=6, column=0, sticky="ew", padx=(0, 3), pady=(0, 6))
        ttk.Button(right, text="Backups", command=lambda: self.notebook.select(self.backups_tab)).grid(row=6, column=1, sticky="ew", padx=(3, 0), pady=(0, 6))
        ttk.Button(right, text="Save diagnostic report…", command=self._save_diagnostic_report).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        right.columnconfigure(0, weight=1)
        right.columnconfigure(1, weight=1)

        capacity = ttk.LabelFrame(self.dashboard_tab, text="Device memory capacity", padding=10)
        capacity.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        capacity.columnconfigure(0, weight=1)
        capacity.columnconfigure(1, weight=1)
        self.dashboard_flash_bar, self.dashboard_flash_capacity_var = self._create_capacity_meter(
            capacity, "Flash", FLASH_SIZE, 0
        )
        self.dashboard_eeprom_bar, self.dashboard_eeprom_capacity_var = self._create_capacity_meter(
            capacity, "EEPROM", EEPROM_SIZE, 1
        )

    def _set_dashboard_report(self, text: str) -> None:
        self.dashboard_text.configure(state="normal")
        self.dashboard_text.delete("1.0", "end")
        self.dashboard_text.insert("1.0", text)
        self.dashboard_text.configure(state="disabled")

    def _connection_stability_test(self) -> None:
        self._run_task(
            "Testing programmer connection",
            lambda: self.service.connection_stability_test(8),
            self._connection_stability_completed,
        )

    def _connection_stability_completed(self, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if not isinstance(outcome, OperationOutcome):
            return
        heading = "CONNECTION TEST PASSED" if outcome.success else "CONNECTION TEST FAILED"
        self._set_dashboard_report(heading + "\n\n" + outcome.detail)
        self._append_log(f"{outcome.title}: {outcome.detail}")
        self.status_var.set(outcome.title)

    def _run_on_chip_self_test(self) -> None:
        if not self._confirm(
            "Temporary on-chip self-test",
            "ATtiny85 Explorer will create a complete recovery backup, temporarily replace Flash, run a hardware self-test inside the ATtiny85, read its EEPROM report, then restore and verify the original Flash and EEPROM.\n\n"
            "The test uses no external pins, but power or USB must not be disconnected during the operation. Locked chips are not modified.",
            phrase="YES",
            button_text="Run test and restore",
        ):
            return
        self._run_task(
            "Running temporary on-chip self-test",
            self.service.on_chip_self_test,
            self._on_chip_self_test_completed,
        )

    def _on_chip_self_test_completed(self, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if not isinstance(outcome, OperationOutcome):
            return
        state = outcome.extra.get("state")
        if isinstance(state, DeviceState) and state.is_attiny85:
            self.current_state = state
            self._update_state_display(state)
        if outcome.extra.get("restored"):
            flash = outcome.extra.get("flash_readback")
            eeprom = outcome.extra.get("eeprom_readback")
            if isinstance(flash, bytes) and len(flash) == FLASH_SIZE:
                self._display_memory_stats("flash", flash, "post-self-test verified restoration", True)
            if isinstance(eeprom, bytes) and len(eeprom) == EEPROM_SIZE:
                self._display_memory_stats("eeprom", eeprom, "post-self-test verified restoration", True)
        heading = "ON-CHIP SELF-TEST PASSED" if outcome.success else "ON-CHIP SELF-TEST NEEDS ATTENTION"
        detail = outcome.detail
        diagnosis = diagnose_results(outcome.run_results, outcome.title)
        if not outcome.success and diagnosis is not None and "Diagnosis:" not in detail:
            detail += "\n\n" + diagnosis.format()
        self._set_dashboard_report(heading + "\n\n" + detail)
        self._append_log(f"{outcome.title}: {detail}")
        if outcome.success:
            messagebox.showinfo(outcome.title, detail, parent=self)
        else:
            messagebox.showerror(outcome.title, detail, parent=self)

    def _read_full_chip_to_workspace(self) -> None:
        dirty = [name for name in ("flash", "eeprom") if self.editor_dirty.get(name, False)]
        if dirty and not messagebox.askyesno(
            "Replace workspace buffers",
            "This will read the connected chip and replace the current Flash and EEPROM editor buffers. "
            "Unsaved editor changes will be lost. Continue?",
            parent=self,
        ):
            return

        def operation():
            state, state_results = self.service.detect()
            if not state.is_attiny85:
                return state, state_results, None, None
            flash = self.service.read_memory("flash")
            eeprom = self.service.read_memory("eeprom")
            return state, state_results, flash, eeprom

        self._run_task("Reading complete ATtiny85 into workspace", operation, self._full_chip_workspace_loaded)

    def _full_chip_workspace_loaded(self, result: object) -> None:
        state, _state_results, flash_outcome, eeprom_outcome = result  # type: ignore[misc]
        self.current_state = state
        self._update_state_display(state)
        if not state.is_attiny85 or flash_outcome is None or eeprom_outcome is None:
            messagebox.showerror(
                "Full-chip read",
                f"Expected ATtiny85 signature 0x1E930B; received {state.signature or 'no signature'}.",
                parent=self,
            )
            return
        failures = []
        for memory, outcome in (("flash", flash_outcome), ("eeprom", eeprom_outcome)):
            if not isinstance(outcome, OperationOutcome) or not outcome.success:
                failures.append(outcome.detail if isinstance(outcome, OperationOutcome) else f"{memory} read failed")
                continue
            data = outcome.extra.get("data", b"")
            if not isinstance(data, bytes):
                failures.append(f"{memory} read returned invalid data")
                continue
            self._display_memory_stats(memory, data, "full-chip workspace read", True)
            self._load_memory_into_editor(memory, data, source="Connected ATtiny85 full-chip read")
        if failures:
            messagebox.showerror("Full-chip read", "\n\n".join(failures), parent=self)
            return
        self._set_dashboard_report(
            "FULL CHIP LOADED INTO WORKSPACE\n\n"
            f"Signature: {state.signature}\n"
            f"Fuses: L={format_byte(state.lfuse)} H={format_byte(state.hfuse)} E={format_byte(state.efuse)}\n"
            f"Lock: {format_byte(state.lock)}\n\n"
            "Flash and EEPROM were read into both the DEVICE displays and editable workspace buffers. "
            "Nothing was written to the chip."
        )
        self.status_var.set("Full ATtiny85 loaded into workspace")

    def _save_diagnostic_report(self) -> None:
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Save ATtiny85 Explorer diagnostic report",
            defaultextension=".txt",
            filetypes=[("Text report", "*.txt"), ("All files", "*.*")],
        )
        if not destination:
            return
        backend = self.registry.preferred()
        state = self.current_state
        lines = [
            f"ATtiny85 Explorer version: {__version__}",
            f"Created: {datetime.now().isoformat(timespec='seconds')}",
            f"Operating system: {platform.platform()}",
            f"Python: {platform.python_version()}",
            f"Interface mode: {self.mode_var.get()}",
            f"Backend: {backend.name if backend else 'None'}",
            f"AVRDUDE executable: {backend.exe_path if backend else '--'}",
            f"AVRDUDE config: {backend.conf_path if backend else '--'}",
            f"ISP speed: {self.isp_speed_var.get()}",
            f"Signature: {state.signature or '--'}",
            f"LFUSE: {format_byte(state.lfuse)}",
            f"HFUSE: {format_byte(state.hfuse)}",
            f"EFUSE: {format_byte(state.efuse)}",
            f"Lock: {format_byte(state.lock)}",
            f"Last AVRDUDE command: {self.last_avrdude_command or '--'}",
            f"Last automatic diagnosis: {self.last_failure_diagnosis or '--'}",
            "",
            "Recent session log (memory contents are not included):",
            *self.log_history[-300:],
        ]
        try:
            Path(destination).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.status_var.set(f"Diagnostic report saved: {destination}")
        except OSError as exc:
            messagebox.showerror("Diagnostic report", str(exc), parent=self)

    # ---------- projects ----------

    def _build_projects_tab(self) -> None:
        self.project_name_var = tk.StringVar(value="Untitled ATtiny85 project")
        self.project_path_var = tk.StringVar(value="No saved project loaded")
        self.project_status_var = tk.StringVar(
            value="Normal ATtiny85 Explorer operation is active. Create or open a project only when you need one."
        )

        # Project packages remain backward compatible with earlier releases, but
        # the component choices are no longer duplicated as editable controls on
        # the Projects tab. Opening a project applies its complete saved workspace.
        self.project_program_flash_var = tk.BooleanVar(value=True)
        self.project_program_eeprom_var = tk.BooleanVar(value=True)
        self.project_apply_fuses_var = tk.BooleanVar(value=True)
        self.project_apply_lock_var = tk.BooleanVar(value=False)
        self.project_lock_target_var = tk.StringVar(value="Unlocked (0xFF)")
        self.project_planned_vcc_var = tk.StringVar(value=str(self.settings.get("attiny85_supply_voltage", SUPPLY_SELECT)))
        self.project_device_grade_var = tk.StringVar(value=str(self.settings.get("attiny85_device_grade", DEVICE_GRADE_SELECT)))

        toolbar = ttk.LabelFrame(self.projects_tab, text="Project file", padding=7)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(6, weight=1)
        ttk.Button(toolbar, text="New", command=self._new_project).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(toolbar, text="Open…", command=self._open_project).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(toolbar, text="Save", command=self._save_project).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(toolbar, text="Save as…", command=self._save_project_as).grid(row=0, column=3, padx=(0, 10))
        ttk.Button(toolbar, text="Capture chip", command=self._capture_chip_for_project).grid(row=0, column=4, padx=(0, 4))
        ttk.Button(toolbar, text="Use workspace", command=self._project_use_current_workspace).grid(row=0, column=5, padx=(0, 4))

        self.project_path_label = ttk.Label(
            toolbar,
            textvariable=self.project_path_var,
            foreground="#555555",
            anchor="w",
            justify="left",
            wraplength=620,
            width=1,
        )
        self.project_path_label.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(6, 0))

        self.project_backup_check = ttk.Checkbutton(
            toolbar,
            text="Create complete recovery backup",
            variable=self.operation_backup_var,
            command=self._project_backup_option_changed,
        )
        self.project_backup_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(
            toolbar,
            text="Shared with Flash and EEPROM programming options",
            foreground="#555555",
        ).grid(row=2, column=3, columnspan=3, sticky="w", padx=(8, 0), pady=(7, 0))
        self.project_program_button = ttk.Button(
            toolbar,
            text="Program saved project + verify…",
            command=self._program_loaded_project,
            state="disabled",
        )
        self.project_program_button.grid(row=2, column=6, padx=(10, 0), pady=(7, 0), sticky="e")

        pane = ttk.Panedwindow(self.projects_tab, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew", pady=(7, 0))
        self.projects_tab.columnconfigure(0, weight=1)
        self.projects_tab.rowconfigure(1, weight=1)

        information = ttk.LabelFrame(pane, text="Project information", padding=8)
        information.columnconfigure(1, weight=1)
        information.rowconfigure(2, weight=1)
        pane.add(information, weight=1)
        ttk.Label(information, text="Name:").grid(row=0, column=0, sticky="w")
        ttk.Entry(information, textvariable=self.project_name_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(
            information,
            text=(
                "Project settings are not selected here. Opening a project automatically loads its Flash and EEPROM "
                "images, fuse setup, chip grade, planned VCC, ISP speed, and verification settings into their normal "
                "ATtiny85 Explorer controls. Nothing is written to the chip until you press Program."
            ),
            wraplength=340,
            foreground="#555555",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 8))
        notes_frame = ttk.LabelFrame(information, text="Notes", padding=4)
        notes_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        notes_frame.columnconfigure(0, weight=1)
        notes_frame.rowconfigure(0, weight=1)
        self.project_notes_text = tk.Text(notes_frame, height=8, width=32, wrap="word")
        self.project_notes_text.grid(row=0, column=0, sticky="nsew")
        notes_scroll = ttk.Scrollbar(notes_frame, orient="vertical", command=self.project_notes_text.yview)
        notes_scroll.grid(row=0, column=1, sticky="ns")
        self.project_notes_text.configure(yscrollcommand=notes_scroll.set)

        summary = ttk.LabelFrame(pane, text="Loaded contents and programming plan", padding=8)
        summary.columnconfigure(0, weight=1)
        summary.rowconfigure(1, weight=1)
        pane.add(summary, weight=1)
        ttk.Label(summary, textvariable=self.project_status_var, style="Status.TLabel", wraplength=420).grid(row=0, column=0, sticky="ew", pady=(0, 5))
        summary_frame = ttk.Frame(summary)
        summary_frame.grid(row=1, column=0, sticky="nsew")
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.rowconfigure(0, weight=1)
        self.project_summary_text = tk.Text(summary_frame, font=("Consolas", 9), width=46, wrap="word")
        project_scroll = ttk.Scrollbar(summary_frame, orient="vertical", command=self.project_summary_text.yview)
        self.project_summary_text.configure(yscrollcommand=project_scroll.set)
        self.project_summary_text.grid(row=0, column=0, sticky="nsew")
        project_scroll.grid(row=0, column=1, sticky="ns")
        self._set_text(
            self.project_summary_text,
            "No project is loaded. The Flash, EEPROM, and Fuses pages continue to work normally.\n\n"
            "Use New to prepare a manually saved project from the current workspace, or Open to load and validate an existing .avrxproj file.",
        )

    def _project_backup_option_changed(self) -> None:
        self._save_operation_options()
        state = "ON" if self.operation_backup_var.get() else "OFF"
        self.project_status_var.set(
            f"Project backup is {state}. This preference is saved and is also used by Flash, EEPROM, fuse, and erase operations."
        )
        self._project_refresh_summary()

    def _sync_project_metadata_from_workspace(self) -> None:
        if hasattr(self, "device_grade_var"):
            self.project_device_grade_var.set(self.device_grade_var.get())
        if hasattr(self, "supply_voltage_var"):
            self.project_planned_vcc_var.set(self.supply_voltage_var.get())

    def _project_use_current_workspace(self) -> None:
        self._sync_project_metadata_from_workspace()
        self.project_status_var.set(
            "Current Flash, EEPROM, fuse setup, chip grade, planned VCC, ISP speed, and verification options are ready to be saved as a project."
        )
        self._project_refresh_summary()

    def _project_lock_toggle(self) -> None:
        # Lock behavior is retained in the project package for compatibility and
        # is shown in the read-only plan instead of as a duplicate selector.
        self._project_refresh_summary()

    def _new_project(self) -> None:
        self.loaded_project = None
        self.loaded_project_path = None
        self.project_name_var.set("Untitled ATtiny85 project")
        self.project_path_var.set("New unsaved project")
        self.project_notes_text.delete("1.0", "end")
        self.project_program_flash_var.set(True)
        self.project_program_eeprom_var.set(True)
        self.project_apply_fuses_var.set(True)
        self.project_apply_lock_var.set(False)
        self.project_lock_target_var.set("Unlocked (0xFF)")
        self._sync_project_metadata_from_workspace()
        self._project_lock_toggle()
        self.project_program_button.configure(state="disabled")
        backup_state = "ON" if self.operation_backup_var.get() else "OFF"
        self.project_status_var.set(
            f"New project draft. Nothing is autosaved; Save or Save as captures the current workspace. Backup before programming is {backup_state}."
        )
        self._project_refresh_summary()

    def _project_fuse_values_from_workspace(self) -> Tuple[int, int, int, int]:
        try:
            lfuse, hfuse, efuse = self._current_fuse_config().encode()
        except Exception:
            if self.current_state.lfuse is None or self.current_state.hfuse is None or self.current_state.efuse is None:
                raise ValueError("The project needs valid proposed fuse values. Read the chip or select a valid fuse preset first.")
            lfuse, hfuse, efuse = self.current_state.lfuse, self.current_state.hfuse, self.current_state.efuse
        lock_map = {
            "Unlocked (0xFF)": 0xFF,
            "Mode 2 protection (0xFE)": 0xFE,
            "Mode 3 protection (0xFC)": 0xFC,
        }
        lock = lock_map.get(self.project_lock_target_var.get(), 0xFF)
        return lfuse, hfuse, efuse, lock

    def _project_workspace_data(self) -> Tuple[bytes, bytes, Tuple[int, int, int, int]]:
        flash = bytes(self.editor_images.get("flash", b""))
        eeprom = bytes(self.editor_images.get("eeprom", b""))
        if len(flash) != FLASH_SIZE or len(eeprom) != EEPROM_SIZE:
            raise ValueError("The Flash and EEPROM workspace buffers are not initialized.")
        return flash, eeprom, self._project_fuse_values_from_workspace()

    def _project_refresh_summary(self) -> None:
        try:
            flash, eeprom, fuses = self._project_workspace_data()
            flash_stats = analyze_memory(flash, FLASH_PAGE_SIZE)
            eeprom_stats = analyze_memory(eeprom, EEPROM_PAGE_SIZE)
            config = FuseConfig.decode(fuses[0], fuses[1], fuses[2])
            source = str(self.loaded_project_path) if self.loaded_project_path else "Unsaved workspace draft"
            grade = self.device_grade_var.get() if hasattr(self, "device_grade_var") else self.project_device_grade_var.get()
            planned_vcc = self.supply_voltage_var.get() if hasattr(self, "supply_voltage_var") else self.project_planned_vcc_var.get()
            backup_enabled = bool(self.operation_backup_var.get())
            text = (
                f"Project: {self.project_name_var.get().strip() or 'Untitled'}\n"
                f"File: {source}\n"
                f"Expected device: ATtiny85 / 0x1E930B\n"
                f"Chip marking: {grade}\n"
                f"Planned operating VCC: {planned_vcc} (informational; the app does not control voltage)\n"
                f"ISP speed: {self.isp_speed_var.get()}\n\n"
                f"Flash: {flash_stats.programmed_bytes:,} programmed bytes of {FLASH_SIZE:,} bytes; SHA-256 {flash_stats.sha256}\n"
                f"EEPROM: {eeprom_stats.programmed_bytes:,} programmed bytes of {EEPROM_SIZE:,} bytes; SHA-256 {eeprom_stats.sha256}\n"
                f"Fuses: LFUSE 0x{fuses[0]:02X}  HFUSE 0x{fuses[1]:02X}  EFUSE 0x{fuses[2]:02X}\n"
                f"Clock: {config.system_clock_description()}\n"
                f"Stored lock byte: 0x{fuses[3]:02X} ({self.project_lock_target_var.get()})\n\n"
                "Saved project programming plan (read-only here):\n"
                f"  Flash: {'YES' if self.project_program_flash_var.get() else 'NO'}\n"
                f"  EEPROM: {'YES' if self.project_program_eeprom_var.get() else 'NO'}\n"
                f"  Fuses: {'YES' if self.project_apply_fuses_var.get() else 'NO'}\n"
                f"  Lock bits: {'YES' if self.project_apply_lock_var.get() else 'NO'}\n"
                f"  Backup before programming: {'YES' if backup_enabled else 'NO'} (current saved app preference)\n"
                f"  AVRDUDE verify during write: {'YES' if self.operation_verify_var.get() else 'NO'}\n"
                f"  Second full readback option: {'YES' if self.operation_full_readback_var.get() else 'NO'}\n"
                "  Project programming always performs its final selected-memory readback before lock protection.\n"
            )
            if not backup_enabled:
                text += "\nWARNING: Backup is OFF. Project programming will explicitly warn again before writing."
            self._set_text(self.project_summary_text, text)
        except Exception as exc:
            self._set_text(self.project_summary_text, f"Project workspace is not ready:\n\n{exc}")

    def _save_project(self) -> None:
        if not self.loaded_project_path:
            self._save_project_as()
            return
        self._write_project_file(self.loaded_project_path)

    def _save_project_as(self) -> None:
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Save ATtiny85 project",
            initialdir=str(PROJECT_DIR),
            initialfile=(self.project_name_var.get().strip() or "ATtiny85-project") + ".avrxproj",
            defaultextension=".avrxproj",
            filetypes=[("ATtiny85 Explorer project", "*.avrxproj"), ("All files", "*.*")],
        )
        if destination:
            self._write_project_file(Path(destination))

    def _write_project_file(self, path: Path) -> None:
        try:
            self._sync_project_metadata_from_workspace()
            flash, eeprom, fuses = self._project_workspace_data()
            saved = create_project_package(
                path,
                name=self.project_name_var.get(),
                notes=self.project_notes_text.get("1.0", "end-1c"),
                flash=flash,
                eeprom=eeprom,
                lfuse=fuses[0],
                hfuse=fuses[1],
                efuse=fuses[2],
                lock=fuses[3],
                device_grade=self.project_device_grade_var.get(),
                planned_vcc=self.project_planned_vcc_var.get(),
                isp_speed=self.isp_speed_var.get(),
                fuse_preset_name=getattr(self, "loaded_preset_name", ""),
                external_frequency_mhz=self._external_frequency_mhz() if hasattr(self, "external_frequency_var") else None,
                program_flash=self.project_program_flash_var.get(),
                program_eeprom=self.project_program_eeprom_var.get(),
                apply_fuses=self.project_apply_fuses_var.get(),
                apply_lock=self.project_apply_lock_var.get(),
                verify_after_write=self.operation_verify_var.get(),
                final_full_readback=self.operation_full_readback_var.get(),
                app_version=__version__,
            )
            package = read_project_package(saved)
            self.loaded_project = package
            self.loaded_project_path = saved
            self.project_path_var.set(str(saved))
            backup_state = "ON" if self.operation_backup_var.get() else "OFF"
            self.project_status_var.set(
                f"Saved and validated. Program uses this saved package. Backup before programming is currently {backup_state}."
            )
            self.project_program_button.configure(state="normal")
            self._project_refresh_summary()
        except Exception as exc:
            messagebox.showerror("Save project", str(exc), parent=self)

    def _open_project(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="Open ATtiny85 project",
            initialdir=str(PROJECT_DIR),
            filetypes=[("ATtiny85 Explorer project", "*.avrxproj"), ("All files", "*.*")],
        )
        if not selected:
            return
        try:
            path = Path(selected)
            package = read_project_package(path)
            manifest = package["manifest"]
            self.loaded_project = package
            self.loaded_project_path = path
            self.project_path_var.set(str(path))
            self.project_name_var.set(str(manifest.get("name", path.stem)))
            self.project_notes_text.delete("1.0", "end")
            self.project_notes_text.insert("1.0", str(manifest.get("notes", "")))

            self.project_device_grade_var.set(str(manifest.get("device_grade", DEVICE_GRADE_SELECT)))
            self.project_planned_vcc_var.set(str(manifest.get("planned_vcc", SUPPLY_SELECT)))
            components = manifest.get("components", {})
            if not isinstance(components, dict):
                components = {}
            self.project_program_flash_var.set(bool(components.get("program_flash", True)))
            self.project_program_eeprom_var.set(bool(components.get("program_eeprom", True)))
            self.project_apply_fuses_var.set(bool(components.get("apply_fuses", True)))
            self.project_apply_lock_var.set(bool(components.get("apply_lock", False)))
            stored_lock = int(manifest.get("fuses", {}).get("lock", 0xFF))
            self.project_lock_target_var.set({
                0xFE: "Mode 2 protection (0xFE)",
                0xFC: "Mode 3 protection (0xFC)",
            }.get(stored_lock, "Unlocked (0xFF)"))
            if hasattr(self, "lock_choice_var"):
                self.lock_choice_var.set({0xFE: "mode2", 0xFC: "mode3"}.get(stored_lock, "unlocked"))

            # Complete project load: memory images and every saved workspace
            # setting are pushed into their normal controls. Backup remains a
            # user preference and is deliberately not overridden by the project.
            self._load_memory_into_editor("flash", package["flash"], source=f"Project: {path.name}")
            self._load_memory_into_editor("eeprom", package["eeprom"], source=f"Project: {path.name}")
            fuses = manifest.get("fuses", {})
            config = FuseConfig.decode(int(fuses["lfuse"]), int(fuses["hfuse"]), int(fuses["efuse"]))
            self._load_config_into_editor(config, mark_dirty=True)
            self.device_grade_var.set(self.project_device_grade_var.get())
            self.supply_voltage_var.set(self.project_planned_vcc_var.get())

            saved_isp_speed = str(manifest.get("isp_speed", "Automatic"))
            valid_isp_speeds = {
                "Automatic", "Fast (1 µs)", "Compatible (10 µs)", "Slow (40 µs)"
            }
            self.isp_speed_var.set(saved_isp_speed if saved_isp_speed in valid_isp_speeds else "Automatic")
            verification = manifest.get("verification", {})
            if not isinstance(verification, dict):
                verification = {}
            self.operation_verify_var.set(bool(verification.get("verify_after_write", True)))
            self.operation_full_readback_var.set(bool(verification.get("final_full_readback", True)))

            fuse_setup = manifest.get("fuse_setup", {})
            if isinstance(fuse_setup, dict):
                self.loaded_preset_name = str(fuse_setup.get("preset_name", ""))
                external_frequency = fuse_setup.get("external_frequency_mhz")
                self.external_frequency_var.set("" if external_frequency is None else str(external_frequency))

            self._save_operation_options()
            self._refresh_fuse_preview()
            backup_state = "ON" if self.operation_backup_var.get() else "OFF"
            self.project_status_var.set(
                "Project loaded and validated. Flash, EEPROM, fuses, chip grade, planned VCC, ISP speed, and "
                f"verification settings were preloaded. The chip was not written. Backup before programming is {backup_state}."
            )
            self.project_program_button.configure(state="normal")
            self._project_refresh_summary()
        except Exception as exc:
            self.loaded_project = None
            self.loaded_project_path = None
            self.project_program_button.configure(state="disabled")
            messagebox.showerror("Open project", str(exc), parent=self)

    def _capture_chip_for_project(self) -> None:
        def completed(result: object) -> None:
            self._full_chip_workspace_loaded(result)
            state = self.current_state
            if state.is_attiny85:
                self.project_name_var.set(f"ATtiny85 capture {datetime.now().strftime('%Y-%m-%d %H-%M-%S')}")
                self.project_lock_target_var.set({
                    0xFE: "Mode 2 protection (0xFE)",
                    0xFC: "Mode 3 protection (0xFC)",
                }.get(state.lock, "Unlocked (0xFF)"))
                # Capturing records the current lock byte, but does not opt into
                # applying protection when the project is programmed.
                self.project_apply_lock_var.set(False)
                self._sync_project_metadata_from_workspace()
                self._project_lock_toggle()
                self.project_status_var.set("Connected chip captured into the workspace. Save manually when the project is ready.")
                self._project_refresh_summary()
                self.notebook.select(self.projects_tab)

        dirty = [name for name in ("flash", "eeprom") if self.editor_dirty.get(name, False)]
        if dirty and not messagebox.askyesno(
            "Capture connected chip",
            "Capturing the chip will replace the current Flash and EEPROM workspace buffers. Continue?",
            parent=self,
        ):
            return

        def operation():
            state, state_results = self.service.detect()
            if not state.is_attiny85:
                return state, state_results, None, None
            return state, state_results, self.service.read_memory("flash"), self.service.read_memory("eeprom")

        self._run_task("Capturing connected ATtiny85", operation, completed)

    def _project_workspace_matches_saved(self) -> bool:
        if not self.loaded_project:
            return False
        try:
            flash, eeprom, fuses = self._project_workspace_data()
            manifest = self.loaded_project["manifest"]
            stored_fuses = manifest["fuses"]
            components = manifest.get("components", {})
            verification = manifest.get("verification", {})
            if not isinstance(components, dict):
                components = {}
            if not isinstance(verification, dict):
                verification = {}
            current_grade = self.device_grade_var.get() if hasattr(self, "device_grade_var") else self.project_device_grade_var.get()
            current_vcc = self.supply_voltage_var.get() if hasattr(self, "supply_voltage_var") else self.project_planned_vcc_var.get()
            return (
                flash == self.loaded_project["flash"]
                and eeprom == self.loaded_project["eeprom"]
                and (fuses[0], fuses[1], fuses[2], fuses[3])
                == (stored_fuses["lfuse"], stored_fuses["hfuse"], stored_fuses["efuse"], stored_fuses["lock"])
                and bool(components.get("program_flash", True)) == self.project_program_flash_var.get()
                and bool(components.get("program_eeprom", True)) == self.project_program_eeprom_var.get()
                and bool(components.get("apply_fuses", True)) == self.project_apply_fuses_var.get()
                and bool(components.get("apply_lock", False)) == self.project_apply_lock_var.get()
                and str(manifest.get("name", "")) == self.project_name_var.get().strip()
                and str(manifest.get("device_grade", "")) == current_grade
                and str(manifest.get("planned_vcc", "")) == current_vcc
                and str(manifest.get("isp_speed", "Automatic")) == self.isp_speed_var.get()
                and bool(verification.get("verify_after_write", True)) == self.operation_verify_var.get()
                and bool(verification.get("final_full_readback", True)) == self.operation_full_readback_var.get()
            )
        except Exception:
            return False

    def _program_loaded_project(self) -> None:
        if not self.loaded_project or not self.loaded_project_path:
            messagebox.showerror("Program project", "Open or save a validated project first.", parent=self)
            return
        if not self._project_workspace_matches_saved():
            messagebox.showerror(
                "Save project changes first",
                "The current workspace or one of the preloaded project settings differs from the saved project. Save the project before programming so the file and the requested operation cannot disagree.",
                parent=self,
            )
            return
        package = self.loaded_project
        manifest = package["manifest"]
        components = manifest.get("components", {})
        if not any(bool(components.get(key, False)) for key in ("program_flash", "program_eeprom", "apply_fuses", "apply_lock")):
            messagebox.showerror("Program project", "This project has no enabled programming components.", parent=self)
            return

        def preflight():
            state, state_results = self.service.detect()
            flash = self.service.read_memory("flash") if bool(components.get("program_flash", True)) else None
            eeprom = self.service.read_memory("eeprom") if bool(components.get("program_eeprom", True)) else None
            return state, state_results, flash, eeprom

        self._run_task("Checking saved project against connected chip", preflight, self._project_preflight_completed)

    def _project_preflight_completed(self, result: object) -> None:
        state, _state_results, flash_outcome, eeprom_outcome = result  # type: ignore[misc]
        if not state.is_attiny85:
            messagebox.showerror("Project preflight", f"Expected ATtiny85 0x1E930B; received {state.signature or 'no signature'}.", parent=self)
            return
        package = self.loaded_project
        if not package:
            return
        manifest = package["manifest"]
        components = manifest.get("components", {})
        if not isinstance(components, dict):
            components = {}
        fuses = manifest["fuses"]
        target_fuses = (int(fuses["lfuse"]), int(fuses["hfuse"]), int(fuses["efuse"]))
        target_lock = int(fuses["lock"])
        config = FuseConfig.decode(*target_fuses)
        current_fuses = (state.lfuse, state.hfuse, state.efuse)
        fuse_changes = bool(components.get("apply_fuses", True)) and current_fuses != target_fuses
        lock_change = bool(components.get("apply_lock", False)) and target_lock in (0xFE, 0xFC) and state.lock != target_lock
        backup_enabled = bool(self.operation_backup_var.get())

        lines = [
            f"Project: {manifest.get('name', self.loaded_project_path.stem if self.loaded_project_path else 'ATtiny85 project')}",
            f"File: {self.loaded_project_path}",
            f"Detected: ATtiny85 {state.signature}",
            f"Complete recovery backup first: {'YES' if backup_enabled else 'NO'}",
            "",
        ]
        if flash_outcome is not None:
            if not flash_outcome.success:
                messagebox.showerror("Project preflight", flash_outcome.detail, parent=self)
                return
            actual = flash_outcome.extra.get("data", b"")
            differences = sum(a != b for a, b in zip(actual, package["flash"]))
            lines.append(f"Flash: {format_bytes(differences)} will differ from the saved project")
        else:
            lines.append("Flash: not selected by the saved project")
        if eeprom_outcome is not None:
            if not eeprom_outcome.success:
                messagebox.showerror("Project preflight", eeprom_outcome.detail, parent=self)
                return
            actual = eeprom_outcome.extra.get("data", b"")
            differences = sum(a != b for a, b in zip(actual, package["eeprom"]))
            lines.append(f"EEPROM: {format_bytes(differences)} will differ from the saved project")
        else:
            lines.append("EEPROM: not selected by the saved project")
        lines.extend([
            f"Fuses: {'will change' if fuse_changes else 'no change or not selected'}",
            f"  Current fuse bytes: LFUSE {format_byte(state.lfuse)} | HFUSE {format_byte(state.hfuse)} | EFUSE {format_byte(state.efuse)}",
            f"  Project fuse bytes: LFUSE 0x{target_fuses[0]:02X} | HFUSE 0x{target_fuses[1]:02X} | EFUSE 0x{target_fuses[2]:02X}",
            f"  Resulting clock: {config.system_clock_description()}",
            f"Lock bits: {'will be applied last' if lock_change else 'no change or not selected'}",
            "",
            "Flash and EEPROM are written and verified, then fuses are written in safe order. A final complete readback of each selected memory must match before optional lock protection is applied.",
        ])
        if backup_enabled:
            lines.append("A complete recovery package will be created before the first write.")
        else:
            lines.extend([
                "",
                "WARNING: Backup is OFF. No recovery package will be created before Flash, EEPROM, or fuse changes.",
                "You can cancel and enable Create complete recovery backup on the Projects, Flash, or EEPROM page.",
            ])

        risks = config.risk_items(raw_override=False)
        planned = str(manifest.get("planned_vcc", SUPPLY_SELECT))
        grade = str(manifest.get("device_grade", DEVICE_GRADE_SELECT))
        fuse_setup = manifest.get("fuse_setup", {})
        preset_name = str(fuse_setup.get("preset_name", "")) if isinstance(fuse_setup, dict) else ""
        external_frequency = fuse_setup.get("external_frequency_mhz") if isinstance(fuse_setup, dict) else None
        report = clock_setup_report(
            config,
            grade,
            planned,
            preset_name,
            voltage_verified=True,
            clock_hardware_ready=True,
            firmware_clock_acknowledged=True,
            external_frequency_mhz=float(external_frequency) if external_frequency is not None else None,
        )
        blocking = list(report.errors)
        if blocking:
            messagebox.showerror(
                "Project clock setup is not safe",
                "The saved project cannot be programmed with its stored operating setup:\n\n" + "\n".join("- " + item for item in blocking),
                parent=self,
            )
            return
        if report.warnings or risks:
            lines.append("\nWarnings:")
            lines.extend("- " + item for item in report.warnings)
            lines.extend(f"- {risk.level}: {risk.title} — {risk.detail}" for risk in risks)
        phrase = "YES" if (not backup_enabled or fuse_changes or lock_change or any(r.level in ("High", "Critical") for r in risks)) else ""
        if not self._confirm(
            "Program saved ATtiny85 project",
            "\n".join(lines),
            phrase=phrase,
            button_text="Program project",
        ):
            return
        self._run_task(
            "Programming and verifying saved project",
            lambda: self._program_project_operation(backup_enabled),
            self._project_programming_completed,
        )

    def _program_project_operation(self, create_backup: bool) -> OperationOutcome:
        package = self.loaded_project
        if not package:
            raise RuntimeError("No project is loaded")
        manifest = package["manifest"]
        components = manifest.get("components", {})
        if not isinstance(components, dict):
            components = {}
        verification = manifest.get("verification", {})
        if not isinstance(verification, dict):
            verification = {}
        verify_during_write = bool(verification.get("verify_after_write", True))
        fuses = manifest["fuses"]
        run_results = []
        backup_path = ""
        original_callback = self.service.progress_callback

        def stage(start: int, end: int, function):
            span = max(1, end - start)
            self.service.progress_callback = lambda p, title, detail="": original_callback(
                start + int(span * max(0, min(100, int(p))) / 100), title, detail
            ) if original_callback else None
            try:
                return function()
            finally:
                self.service.progress_callback = original_callback

        def attach_backup_status(outcome: OperationOutcome) -> OperationOutcome:
            outcome.backup_path = backup_path
            if not create_backup:
                outcome.detail = outcome.detail.rstrip() + "\n\nNo safety backup was created because Backup first was disabled."
            elif backup_path and "backup" not in outcome.detail.lower():
                outcome.detail = outcome.detail.rstrip() + f"\n\nSafety backup: {backup_path}"
            return outcome

        if create_backup:
            ranges = {
                "flash": (18, 43), "eeprom": (43, 63), "fuses": (63, 76),
                "flash_read": (76, 86), "eeprom_read": (86, 94), "lock": (94, 98), "detect": (98, 100),
            }
            backup = stage(0, 18, self.service.backup_now)
            run_results.extend(backup.run_results)
            if not backup.success:
                return backup
            backup_path = backup.backup_path
        else:
            ranges = {
                "flash": (0, 30), "eeprom": (30, 55), "fuses": (55, 70),
                "flash_read": (70, 82), "eeprom_read": (82, 94), "lock": (94, 98), "detect": (98, 100),
            }

        if bool(components.get("program_flash", True)):
            outcome = stage(*ranges["flash"], lambda: self.service.write_flash_data(
                package["flash"], create_backup=False, verify_after_write=verify_during_write,
                full_readback=False, restore_eeprom=False,
            ))
            run_results.extend(outcome.run_results)
            if not outcome.success:
                return attach_backup_status(outcome)
        if bool(components.get("program_eeprom", True)):
            outcome = stage(*ranges["eeprom"], lambda: self.service.write_eeprom_smart_data(
                package["eeprom"], create_backup=False, verify_after_write=verify_during_write,
                full_readback=False,
            ))
            run_results.extend(outcome.run_results)
            if not outcome.success:
                return attach_backup_status(outcome)
        if bool(components.get("apply_fuses", True)):
            target = (int(fuses["lfuse"]), int(fuses["hfuse"]), int(fuses["efuse"]))
            config = FuseConfig.decode(*target)
            outcome = stage(*ranges["fuses"], lambda: self.service.write_fuses(config, raw_values=target, create_backup=False))
            run_results.extend(outcome.run_results)
            if not outcome.success:
                return attach_backup_status(outcome)

        flash_read = stage(*ranges["flash_read"], lambda: self.service.read_memory("flash")) if bool(components.get("program_flash", True)) else None
        eeprom_read = stage(*ranges["eeprom_read"], lambda: self.service.read_memory("eeprom")) if bool(components.get("program_eeprom", True)) else None
        backup_note = f"Safety backup: {backup_path}" if backup_path else "No safety backup was created."
        if flash_read is not None:
            run_results.extend(flash_read.run_results)
            if not flash_read.success or flash_read.extra.get("data") != package["flash"]:
                return OperationOutcome(
                    False,
                    "Project Flash verification failed",
                    f"Final Flash readback did not match. {backup_note}",
                    run_results,
                    backup_path=backup_path,
                )
        if eeprom_read is not None:
            run_results.extend(eeprom_read.run_results)
            if not eeprom_read.success or eeprom_read.extra.get("data") != package["eeprom"]:
                return OperationOutcome(
                    False,
                    "Project EEPROM verification failed",
                    f"Final EEPROM readback did not match. {backup_note}",
                    run_results,
                    backup_path=backup_path,
                )

        lock_value = int(fuses.get("lock", 0xFF))
        if bool(components.get("apply_lock", False)) and lock_value in (0xFE, 0xFC):
            lock_outcome = stage(*ranges["lock"], lambda: self.service.write_lock(lock_value, create_backup=False))
            run_results.extend(lock_outcome.run_results)
            if not lock_outcome.success:
                return attach_backup_status(lock_outcome)

        final_state, final_results = stage(*ranges["detect"], self.service.detect)
        run_results.extend(final_results)
        return OperationOutcome(
            True,
            "Project programmed and verified",
            f"The saved project was programmed and final Flash/EEPROM readback matched. {backup_note}",
            run_results,
            backup_path=backup_path,
            extra={
                "state": final_state,
                "flash_readback": flash_read.extra.get("data") if flash_read is not None else b"",
                "eeprom_readback": eeprom_read.extra.get("data") if eeprom_read is not None else b"",
                "backup_created": bool(backup_path),
            },
        )

    def _project_programming_completed(self, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if not isinstance(outcome, OperationOutcome):
            return
        if outcome.success:
            state = outcome.extra.get("state")
            if isinstance(state, DeviceState):
                self.current_state = state
                self._update_state_display(state)
            flash = outcome.extra.get("flash_readback")
            eeprom = outcome.extra.get("eeprom_readback")
            if isinstance(flash, bytes) and flash:
                self._display_memory_stats("flash", flash, "project final readback", True)
            if isinstance(eeprom, bytes) and eeprom:
                self._display_memory_stats("eeprom", eeprom, "project final readback", True)
            backup_text = "Recovery backup created." if outcome.backup_path else "Backup was disabled; no recovery package was created."
            self.project_status_var.set(
                f"PROGRAMMED AND VERIFIED — saved project, connected chip, and final readback match. {backup_text}"
            )
        else:
            backup_text = "Review the safety backup before retrying." if outcome.backup_path else "No safety backup was created."
            self.project_status_var.set(f"Project programming was not fully verified. {backup_text}")
        self._show_outcome(outcome)
        self._project_refresh_summary()

    # ---------- backup manager ----------

    def _build_backups_tab(self) -> None:
        toolbar = ttk.LabelFrame(self.backups_tab, text="Backup manager", padding=7)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(toolbar, text="Refresh", command=self._refresh_backup_manager).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(toolbar, text="Create backup", command=self._backup_from_manager).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(toolbar, text="Validate package", command=self._validate_selected_backup).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(toolbar, text="Load into workspace", command=self._load_backup_into_workspace).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(toolbar, text="Compare with chip", command=self._compare_backup_with_chip).grid(row=0, column=4, padx=(0, 4))
        ttk.Button(toolbar, text="Restore selected…", command=self._restore_selected_backup).grid(row=0, column=5, padx=(0, 4))
        ttk.Button(toolbar, text="Delete selected…", command=self._delete_selected_backup).grid(row=0, column=6, padx=(0, 4))
        ttk.Button(toolbar, text="Open backup folder", command=self._open_backup_folder).grid(row=0, column=7)

        pane = ttk.Panedwindow(self.backups_tab, orient="vertical")
        pane.grid(row=1, column=0, sticky="nsew", pady=(7, 0))
        self.backups_tab.columnconfigure(0, weight=1)
        self.backups_tab.rowconfigure(1, weight=1)

        list_frame = ttk.LabelFrame(pane, text="Saved backup packages", padding=5)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        pane.add(list_frame, weight=3)
        columns = ("created", "operation", "flash", "eeprom", "fuses", "file")
        self.backup_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "created": "Created",
            "operation": "Reason",
            "flash": "Flash used (bytes)",
            "eeprom": "EEPROM used (bytes)",
            "fuses": "Fuses / lock",
            "file": "File",
        }
        widths = {"created": 145, "operation": 170, "flash": 135, "eeprom": 135, "fuses": 210, "file": 270}
        for column in columns:
            self.backup_tree.heading(column, text=headings[column])
            self.backup_tree.column(column, width=widths[column], anchor="w")
        tree_y = ttk.Scrollbar(list_frame, orient="vertical", command=self.backup_tree.yview)
        tree_x = ttk.Scrollbar(list_frame, orient="horizontal", command=self.backup_tree.xview)
        self.backup_tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.backup_tree.grid(row=0, column=0, sticky="nsew")
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x.grid(row=1, column=0, sticky="ew")
        self.backup_tree.bind("<<TreeviewSelect>>", lambda _event: self._backup_selection_changed())
        self.backup_path_by_item: Dict[str, Path] = {}

        details = ttk.LabelFrame(pane, text="Selected backup details", padding=6)
        details.columnconfigure(0, weight=1)
        details.rowconfigure(0, weight=1)
        pane.add(details, weight=2)
        self.backup_details_text = tk.Text(details, font=("Consolas", 9), wrap="word", height=9)
        detail_y = ttk.Scrollbar(details, orient="vertical", command=self.backup_details_text.yview)
        self.backup_details_text.configure(yscrollcommand=detail_y.set)
        self.backup_details_text.grid(row=0, column=0, sticky="nsew")
        detail_y.grid(row=0, column=1, sticky="ns")
        self._set_text(
            self.backup_details_text,
            "Backups are created only when you request one or before a protected write. Select a package to inspect it."
        )
        self.after_idle(self._refresh_backup_manager)

    def _refresh_backup_manager(self, select_path: Optional[Path] = None) -> None:
        if not hasattr(self, "backup_tree"):
            return
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        self.backup_path_by_item.clear()
        paths = sorted(BACKUP_DIR.glob("*.avrxpkg"), key=lambda item: item.stat().st_mtime, reverse=True)
        selected_item = ""
        for path in paths:
            try:
                package = read_backup_package(path)
                manifest = package["manifest"]
                flash_stats = analyze_memory(package["flash"], FLASH_PAGE_SIZE)
                eeprom_stats = analyze_memory(package["eeprom"], EEPROM_PAGE_SIZE)
                fuses = manifest.get("fuses", {}) if isinstance(manifest, dict) else {}
                created = str(manifest.get("created_at", "--")) if isinstance(manifest, dict) else "--"
                operation = str(manifest.get("operation", "--")) if isinstance(manifest, dict) else "--"
                fuse_text = (
                    f"L {format_byte(fuses.get('lfuse'))} H {format_byte(fuses.get('hfuse'))} "
                    f"E {format_byte(fuses.get('efuse'))} Lock {format_byte(fuses.get('lock'))}"
                ) if isinstance(fuses, dict) else "invalid"
                values = (
                    created.replace("T", " "), operation,
                    f"{flash_stats.programmed_bytes:,} / {FLASH_SIZE:,} bytes",
                    f"{eeprom_stats.programmed_bytes:,} / {EEPROM_SIZE:,} bytes",
                    fuse_text, path.name,
                )
            except Exception as exc:
                values = ("INVALID", str(exc), "--", "--", "--", path.name)
            item = self.backup_tree.insert("", "end", values=values)
            self.backup_path_by_item[item] = path
            if select_path and path.resolve() == select_path.resolve():
                selected_item = item
        if selected_item:
            self.backup_tree.selection_set(selected_item)
            self.backup_tree.see(selected_item)
            self._backup_selection_changed()
        elif paths:
            first = self.backup_tree.get_children()[0]
            self.backup_tree.selection_set(first)
            self._backup_selection_changed()
        else:
            self.selected_backup_path = None
            self._set_text(self.backup_details_text, f"No .avrxpkg files are present in:\n{BACKUP_DIR}")

    def _selected_backup(self) -> Optional[Path]:
        selection = self.backup_tree.selection() if hasattr(self, "backup_tree") else ()
        if not selection:
            return None
        return self.backup_path_by_item.get(selection[0])

    def _backup_selection_changed(self) -> None:
        path = self._selected_backup()
        self.selected_backup_path = path
        if not path:
            return
        try:
            package = read_backup_package(path)
            manifest = package["manifest"]
            flash_stats = analyze_memory(package["flash"], FLASH_PAGE_SIZE)
            eeprom_stats = analyze_memory(package["eeprom"], EEPROM_PAGE_SIZE)
            fuses = manifest.get("fuses", {})
            text = (
                f"File: {path}\n"
                f"Created: {manifest.get('created_at', '--')}\n"
                f"Reason: {manifest.get('operation', '--')}\n"
                f"Observed signature: {manifest.get('observed_signature', '--')}\n"
                f"Backend: {manifest.get('backend', {}).get('name', '--') if isinstance(manifest.get('backend'), dict) else '--'}\n\n"
                f"Flash: {flash_stats.programmed_bytes:,} programmed bytes of {FLASH_SIZE:,} bytes; SHA-256 {flash_stats.sha256}\n"
                f"EEPROM: {eeprom_stats.programmed_bytes:,} programmed bytes of {EEPROM_SIZE:,} bytes; SHA-256 {eeprom_stats.sha256}\n"
                f"LFUSE {format_byte(fuses.get('lfuse'))}  HFUSE {format_byte(fuses.get('hfuse'))}  EFUSE {format_byte(fuses.get('efuse'))}\n"
                f"Lock {format_byte(fuses.get('lock'))}\n\n"
                "Validate checks package sizes and hashes. Load into workspace is read-only. Restore creates another safety backup first."
            )
            self._set_text(self.backup_details_text, text)
        except Exception as exc:
            self._set_text(self.backup_details_text, f"This backup could not be read:\n\n{exc}")

    def _backup_from_manager(self) -> None:
        self._run_task("Creating complete backup", self.service.backup_now, self._backup_manager_created)

    def _backup_manager_created(self, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if not isinstance(outcome, OperationOutcome):
            return
        path = Path(outcome.backup_path) if outcome.backup_path else None
        self._append_log(f"{outcome.title}: {outcome.detail}")
        self._refresh_backup_manager(path)
        if outcome.success:
            self.notebook.select(self.backups_tab)
            self.status_var.set("Backup created and listed")
        else:
            self._show_outcome(outcome)

    def _validate_selected_backup(self) -> None:
        path = self._selected_backup()
        if not path:
            self.status_var.set("Select a backup package first")
            return
        try:
            package = validate_backup_package(path)
            manifest = package["manifest"]
            self._set_text(
                self.backup_details_text,
                self.backup_details_text.get("1.0", "end-1c")
                + f"\n\nVALIDATION PASSED\nPackage version {manifest.get('package_version', '--')}; memory sizes and SHA-256 hashes match.",
            )
            self.status_var.set("Backup package validation passed")
        except Exception as exc:
            self._set_text(self.backup_details_text, f"VALIDATION FAILED\n\n{exc}\n\nFile: {path}")
            self.status_var.set("Backup package validation failed")

    def _load_backup_into_workspace(self) -> None:
        path = self._selected_backup()
        if not path:
            self.status_var.set("Select a backup package first")
            return
        if (self.editor_dirty.get("flash") or self.editor_dirty.get("eeprom")) and not messagebox.askyesno(
            "Replace workspace buffers",
            "Loading this backup will replace unsaved Flash and EEPROM editor changes. Continue?",
            parent=self,
        ):
            return
        try:
            package = validate_backup_package(path)
            manifest = package["manifest"]
            self._load_memory_into_editor("flash", package["flash"], source=f"Backup: {path.name}")
            self._load_memory_into_editor("eeprom", package["eeprom"], source=f"Backup: {path.name}")
            fuses = manifest.get("fuses", {})
            if all(isinstance(fuses.get(key), int) for key in ("lfuse", "hfuse", "efuse")):
                self._load_config_into_editor(FuseConfig.decode(fuses["lfuse"], fuses["hfuse"], fuses["efuse"]), mark_dirty=True)
                self._refresh_fuse_preview()
            self.status_var.set("Backup loaded into workspace; the chip was not changed")
            self.notebook.select(self.flash_tab)
        except Exception as exc:
            messagebox.showerror("Load backup into workspace", str(exc), parent=self)

    def _compare_backup_with_chip(self) -> None:
        path = self._selected_backup()
        if not path:
            self.status_var.set("Select a backup package first")
            return
        try:
            package = validate_backup_package(path)
        except Exception as exc:
            messagebox.showerror("Compare backup", str(exc), parent=self)
            return

        def operation():
            state, state_results = self.service.detect()
            flash = self.service.read_memory("flash")
            eeprom = self.service.read_memory("eeprom")
            return state, state_results, flash, eeprom

        self._run_task(
            "Comparing selected backup with connected chip",
            operation,
            lambda result: self._backup_comparison_completed(path, package, result),
        )

    def _backup_comparison_completed(self, path: Path, package: Dict[str, object], result: object) -> None:
        state, _state_results, flash, eeprom = result  # type: ignore[misc]
        if not state.is_attiny85 or not flash.success or not eeprom.success:
            messagebox.showerror("Compare backup", "The connected ATtiny85 could not be read completely.", parent=self)
            return
        flash_data = flash.extra.get("data", b"")
        eeprom_data = eeprom.extra.get("data", b"")
        flash_diff = sum(a != b for a, b in zip(flash_data, package["flash"]))
        eeprom_diff = sum(a != b for a, b in zip(eeprom_data, package["eeprom"]))
        fuses = package["manifest"].get("fuses", {})
        fuse_match = (
            state.lfuse == fuses.get("lfuse") and state.hfuse == fuses.get("hfuse")
            and state.efuse == fuses.get("efuse") and state.lock == fuses.get("lock")
        )
        text = (
            f"BACKUP COMPARISON\n\nFile: {path}\nDetected: {state.signature}\n\n"
            f"Flash differences: {flash_diff}\nEEPROM differences: {eeprom_diff}\n"
            f"Fuse/lock values match: {'YES' if fuse_match else 'NO'}\n\n"
            + ("CONNECTED CHIP MATCHES THIS BACKUP." if flash_diff == 0 and eeprom_diff == 0 and fuse_match else "The connected chip does not exactly match this backup.")
        )
        self._set_text(self.backup_details_text, text)
        self.status_var.set("Backup comparison complete")

    def _restore_selected_backup(self) -> None:
        path = self._selected_backup()
        if not path:
            self.status_var.set("Select a backup package first")
            return
        try:
            validate_backup_package(path)
        except Exception as exc:
            messagebox.showerror("Restore backup", str(exc), parent=self)
            return
        if not self._confirm(
            "Restore selected backup",
            f"Package: {path}\n\nThis will replace Flash and EEPROM, restore fuse settings, and optionally restore stored lock protection. A new safety backup is created first.",
            phrase="YES",
            button_text="Restore and verify",
        ):
            return
        self._run_task("Restoring selected backup", lambda: self.service.restore_backup(path), self._backup_restore_completed)

    def _backup_restore_completed(self, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if isinstance(outcome, OperationOutcome):
            self._show_outcome(outcome)
            self._refresh_backup_manager(Path(outcome.backup_path) if outcome.backup_path else None)
            if outcome.success:
                self._detect()

    def _delete_selected_backup(self) -> None:
        path = self._selected_backup()
        if not path:
            self.status_var.set("Select a backup package first")
            return
        if not messagebox.askyesno(
            "Delete backup package",
            f"Permanently delete this backup?\n\n{path}",
            parent=self,
        ):
            return
        try:
            path.unlink()
            self.selected_backup_path = None
            self._refresh_backup_manager()
            self.status_var.set("Backup deleted")
        except OSError as exc:
            messagebox.showerror("Delete backup", str(exc), parent=self)

    def _open_backup_folder(self) -> None:
        try:
            os.startfile(str(BACKUP_DIR))
        except Exception:
            self.status_var.set(str(BACKUP_DIR))

    def _create_capacity_meter(self, parent: tk.Misc, title: str, capacity: int, column: int):
        frame = ttk.Frame(parent, padding=(6, 2))
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=title, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        label_var = tk.StringVar(value=f"Not read — {format_capacity(capacity)} total")
        ttk.Label(frame, textvariable=label_var, foreground="#555555").grid(row=0, column=1, sticky="e")
        bar = ttk.Progressbar(frame, mode="determinate", maximum=capacity, value=0, style="Memory.Horizontal.TProgressbar")
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        return bar, label_var

    def _set_capacity_meter(self, bar: ttk.Progressbar, label_var: tk.StringVar, stats: MemoryStats, source: str) -> None:
        bar.configure(maximum=max(1, stats.capacity), value=stats.programmed_bytes)
        label_var.set(
            f"{stats.programmed_bytes:,} / {stats.capacity:,} bytes used "
            f"({stats.used_percent:.1f}%) — {source}"
        )

    def _display_memory_stats(self, memory: str, data: bytes, source: str, device_read: bool) -> MemoryStats:
        page_size = FLASH_PAGE_SIZE if memory == "flash" else EEPROM_PAGE_SIZE
        stats = analyze_memory(data, page_size)
        if device_read:
            self.memory_images[memory] = data
            self.memory_original_images[memory] = data
            self.memory_statistics[memory] = stats
            self._update_device_memory_overview(memory, stats, source)
            self._set_text(getattr(self, f"{memory}_device_view_text"), self._hex_readout_text(data))
            if memory == "flash":
                self._set_capacity_meter(self.dashboard_flash_bar, self.dashboard_flash_capacity_var, stats, source)
            else:
                self._set_capacity_meter(self.dashboard_eeprom_bar, self.dashboard_eeprom_capacity_var, stats, source)
            if self.current_state.signature:
                self._update_state_display(self.current_state)
        return stats

    def _analyze_all_memory(self) -> None:
        def operation():
            return self.service.read_memory("flash"), self.service.read_memory("eeprom")
        self._run_task("Reading flash and EEPROM", operation, self._all_memory_analyzed)

    def _all_memory_analyzed(self, result: object) -> None:
        flash_outcome, eeprom_outcome = result  # type: ignore[misc]
        failures = []
        for memory, outcome in (("flash", flash_outcome), ("eeprom", eeprom_outcome)):
            if outcome.success:
                data = outcome.extra.get("data", b"")
                self._display_memory_stats(memory, data, "device readback", True)
                self._editor_status(
                    memory,
                    "Device readback updated on the left. The file/editor buffer on the right was left unchanged; use Copy device when you want it.",
                )
                self._append_log(outcome.detail)
            else:
                failures.append(f"{memory}: {outcome.detail}")
        if failures:
            messagebox.showerror("Memory analysis", "\n\n".join(failures), parent=self)
        else:
            messagebox.showinfo(
                "Memory analysis complete",
                "Flash and EEPROM were read without being changed. Both DEVICE panes are current; the editor buffers were left untouched.",
                parent=self,
            )

    def _detect(self) -> None:
        self._run_task("Detecting ATtiny85", self.service.detect, self._detected)

    def _detected(self, result: object) -> None:
        state, run_results = result  # type: ignore[misc]
        self.current_state = state
        backend = self.registry.preferred()
        if run_results:
            last = run_results[-1]
            if last.version and backend:
                backend.detected_version = last.version
                self.registry.save()
        self._update_state_display(state)
        if state.is_attiny85:
            self.status_var.set("ATtiny85 ready")
        else:
            diagnosis = diagnose_results(run_results, "target detection")
            detail = f"Expected signature 0x1E930B. Received: {state.signature or 'no signature'}\n\nSee Logs for raw AVRDUDE output."
            if diagnosis is not None:
                detail += "\n\n" + diagnosis.format()
                self.last_failure_diagnosis = diagnosis.format()
            messagebox.showerror("Target detection failed", detail, parent=self)

    def _update_state_display(self, state: DeviceState) -> None:
        backend = self.registry.preferred()
        self.backend_var.set(state.backend_name or (backend.name if backend else "No backend"))
        self.target_var.set("ATtiny85" if state.is_attiny85 else "Not detected")
        self.signature_var.set(state.signature or "--")
        self.fuse_status_var.set(f"L {format_byte(state.lfuse)}  H {format_byte(state.hfuse)}  E {format_byte(state.efuse)}")
        self.lock_status_var.set(format_byte(state.lock))
        clock_text = "--"
        detail_lines = [
            f"Programmer: FabISP / USBtinyISP",
            f"Backend: {state.backend_name or '--'}",
            f"Target: {'ATtiny85' if state.is_attiny85 else 'Not identified'}",
            f"Signature: {state.signature or '--'}",
            "Flash: 8192 bytes",
            "EEPROM: 512 bytes",
            "SRAM: 512 bytes",
            f"LFUSE: {format_byte(state.lfuse)}",
            f"HFUSE: {format_byte(state.hfuse)}",
            f"EFUSE: {format_byte(state.efuse)}",
            f"Lock byte: {format_byte(state.lock)}",
        ]
        if state.lfuse is not None and state.hfuse is not None and state.efuse is not None:
            config = FuseConfig.decode(state.lfuse, state.hfuse, state.efuse)
            clock_text = config.system_clock_description()
            detail_lines.extend([
                f"Clock: {clock_text}",
                f"Brown-out: {config.bod}",
                f"EEPROM preserved on chip erase: {'Yes' if config.preserve_eeprom else 'No'}",
                f"RESET disabled: {'Yes' if config.reset_disabled else 'No'}",
                f"debugWIRE enabled: {'Yes' if config.debugwire else 'No'}",
            ])
            audit = audit_current_fuses(
                state,
                str(self.settings.get("attiny85_device_grade", DEVICE_GRADE_SELECT)),
                str(self.settings.get("attiny85_supply_voltage", SUPPLY_SELECT)),
            )
            if audit:
                detail_lines.extend(["", "Configuration safeguards:"] + ["- " + item for item in audit])
            else:
                detail_lines.extend(["", "Configuration safeguards: no conflict found in the information currently available."])
            if not getattr(self, "fuse_editor_dirty", False):
                self._load_config_into_editor(config, mark_dirty=False)
                if hasattr(self, "fuse_editor_status_var"):
                    self.fuse_editor_status_var.set("Editor synchronized to the fuse values read from the chip.")
            elif hasattr(self, "fuse_editor_status_var"):
                self.fuse_editor_status_var.set(
                    "Chip fuse readback updated the Current line. Your unprogrammed proposed values were preserved in the editor."
                )
        if state.lock is not None:
            detail_lines.append(f"Lock status: {decode_lock(state.lock)}")
        for memory_name, label in (("flash", "Flash"), ("eeprom", "EEPROM")):
            stats = self.memory_statistics.get(memory_name)
            if stats is not None:
                page_size = FLASH_PAGE_SIZE if memory_name == "flash" else EEPROM_PAGE_SIZE
                detail_lines.extend([
                    "",
                    f"{label} programmed bytes: {stats.programmed_bytes:,} / {stats.capacity:,} bytes ({stats.used_percent:.2f}%)",
                    f"{label} erased/free bytes: {stats.erased_bytes:,} bytes",
                    f"{label} occupied address span: {stats.occupied_span:,} bytes",
                    f"{label} programmed pages: {format_pages(stats.programmed_pages, stats.total_pages, page_size)}",
                ])
        detail_lines.append("\nHealth: READY" if state.is_attiny85 else "\nHealth: NOT READY")
        self.clock_status_var.set(clock_text)
        if hasattr(self, "operation_options_summary_var"):
            self._update_operation_options_summary()
        self.dashboard_text.configure(state="normal")
        self.dashboard_text.delete("1.0", "end")
        self.dashboard_text.insert("1.0", "\n".join(detail_lines))
        self.dashboard_text.configure(state="disabled")
        self.current_fuse_label.configure(
            text=f"Current: LFUSE {format_byte(state.lfuse)}   HFUSE {format_byte(state.hfuse)}   EFUSE {format_byte(state.efuse)}"
        )
        self.current_lock_var.set(format_byte(state.lock))
        if state.lock is not None:
            self.lock_description_var.set(decode_lock(state.lock))
            if (state.lock & 0x03) == 0x03:
                self.lock_choice_var.set("unlocked")
        if hasattr(self, "proposed_fuse_label"):
            self._refresh_fuse_preview()

    def _backup_now(self) -> None:
        self._run_task("Creating complete backup", self.service.backup_now)

    def _restore_backup(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Select ATtiny85 Explorer backup",
            filetypes=[("ATtiny85 Explorer package", "*.avrxpkg"), ("All files", "*.*")],
        )
        if not path:
            return
        if not self._confirm(
            "Restore backup",
            "This will erase and replace flash and EEPROM, then restore fuse settings from the package.\n\n"
            "A new safety backup will be created first.",
            phrase="RESTORE BACKUP",
            button_text="Restore",
        ):
            return
        self._run_task("Restoring backup", lambda: self.service.restore_backup(Path(path)))

    @staticmethod
    def _hex_readout_text(data: bytes) -> str:
        return editor_text_from_memory(data, "Addressed hex")

    def _build_memory_workspace(self, tab: ttk.Frame, memory: str) -> ttk.Panedwindow:
        """Create the resizable DEVICE / FILE-EDITOR dashboard host."""
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        workspace = ttk.Panedwindow(tab, orient="vertical")
        workspace.grid(row=0, column=0, sticky="nsew")
        dashboard_host = ttk.Frame(workspace)
        dashboard_host.columnconfigure(0, weight=1)
        dashboard_host.rowconfigure(0, weight=1)
        workspace.add(dashboard_host, weight=3)
        setattr(self, f"{memory}_workspace_pane", workspace)
        setattr(self, f"{memory}_dashboard_host", dashboard_host)
        self._build_embedded_memory_viewer(dashboard_host, memory)
        return workspace

    def _restore_single_panel_layout(self, memory: str) -> None:
        dashboard = getattr(self, f"{memory}_dashboard_pane", None)
        workspace = getattr(self, f"{memory}_workspace_pane", None)
        if dashboard is not None and len(dashboard.panes()) > 1:
            width = max(1, dashboard.winfo_width())
            default = int(width * 0.44)
            position = int(self.settings.get(f"{memory}_dashboard_split", default))
            if width >= 560:
                if position < 260 or position > width - 260:
                    position = default
            else:
                position = default
            try:
                dashboard.sashpos(0, position)
            except tk.TclError:
                pass
        if workspace is not None and len(workspace.panes()) > 1:
            height = max(1, workspace.winfo_height())
            default = int(height * 0.60)
            position = int(self.settings.get(f"{memory}_workspace_split", default))
            if height >= 500:
                if position < 300 or position > height - 180:
                    position = default
            elif height >= 430:
                if position < 240 or position > height - 160:
                    position = default
            else:
                position = default
            try:
                workspace.sashpos(0, position)
            except tk.TclError:
                pass

    def _memory_tab_became_visible(self, _event: tk.Event) -> None:
        try:
            selected = self.notebook.nametowidget(self.notebook.select())
        except (tk.TclError, KeyError):
            return
        if selected is self.backups_tab:
            self._refresh_backup_manager()
            return
        memory = "flash" if selected is self.flash_tab else "eeprom" if selected is self.eeprom_tab else ""
        if not memory:
            return
        self.update_idletasks()
        # Tk keeps a Panedwindow sash where the user dragged it. Restore the
        # saved/default position only the first time this memory page is shown
        # during the current application session.
        if not self._memory_layout_initialized.get(memory, False):
            self._restore_single_panel_layout(memory)
            self._memory_layout_initialized[memory] = True

    def _restore_panel_layout(self) -> None:
        for memory in ("flash", "eeprom"):
            if hasattr(self, f"{memory}_dashboard_pane"):
                self._restore_single_panel_layout(memory)

    def _reset_panel_layout(self) -> None:
        for memory in ("flash", "eeprom"):
            dashboard = getattr(self, f"{memory}_dashboard_pane", None)
            workspace = getattr(self, f"{memory}_workspace_pane", None)
            if dashboard is not None and len(dashboard.panes()) > 1:
                try:
                    dashboard.sashpos(0, max(280, int(dashboard.winfo_width() * 0.44)))
                except tk.TclError:
                    pass
            if workspace is not None and len(workspace.panes()) > 1:
                try:
                    workspace.sashpos(0, max(300, int(workspace.winfo_height() * 0.58)))
                except tk.TclError:
                    pass
        self._memory_layout_initialized["flash"] = True
        self._memory_layout_initialized["eeprom"] = True
        self.status_var.set("Panel sizes reset. Drag any divider to resize the views.")

    def _build_memory_overview(self, parent: tk.Misc, memory: str, capacity: int) -> None:
        """Compatibility wrapper retained for older add-ons.

        Version 0.4.7 builds the capacity displays directly inside the two
        source panels created by _build_embedded_memory_viewer().
        """
        if not hasattr(self, f"{memory}_device_capacity_var"):
            self._build_embedded_memory_viewer(parent, memory)

    def _build_embedded_memory_viewer(self, parent: tk.Misc, memory: str) -> None:
        capacity = FLASH_SIZE if memory == "flash" else EEPROM_SIZE
        dashboard = ttk.Panedwindow(parent, orient="horizontal")
        dashboard.grid(row=0, column=0, sticky="nsew")
        setattr(self, f"{memory}_dashboard_pane", dashboard)

        definitions = (
            (
                "device",
                "DEVICE — Connected ATtiny85",
                f"Not read — {format_capacity(capacity)} total",
                "Read the chip to display its actual contents here.",
                "No device readback yet. Use Read device to load the complete memory from the connected ATtiny85.",
            ),
            (
                "file",
                "FILE / EDITOR — inspect, edit, save, or program",
                f"0 / {capacity:,} bytes programmed (blank editor)",
                f"Blank 0xFF buffer — {format_capacity(capacity)} total. Load a file or copy the device readback, then type over hex bytes or the ASCII column.",
                "",
            ),
        )

        for source, title, initial, initial_detail, initial_text in definitions:
            panel = ttk.LabelFrame(dashboard, text=title, padding=7)
            panel.columnconfigure(0, weight=1)
            text_row = 6 if source == "file" else 5
            panel.rowconfigure(text_row, weight=1)
            dashboard.add(panel, weight=1)

            status_var = tk.StringVar(value=initial)
            ttk.Label(panel, textvariable=status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
            bar = ttk.Progressbar(
                panel,
                maximum=capacity,
                mode="determinate",
                style="Memory.Horizontal.TProgressbar",
            )
            bar.grid(row=1, column=0, sticky="ew", pady=(3, 0))
            detail_var = tk.StringVar(value=initial_detail)
            ttk.Label(
                panel,
                textvariable=detail_var,
                foreground="#555555",
                wraplength=400,
                justify="left",
            ).grid(row=2, column=0, sticky="ew", pady=(3, 0))

            controls = ttk.Frame(panel)
            controls.grid(row=3, column=0, sticky="ew", pady=(5, 0))
            controls.columnconfigure(0, weight=1)

            if source == "file":
                status_line = ttk.Frame(panel)
                status_line.grid(row=4, column=0, sticky="ew", pady=(4, 0))
                status_line.columnconfigure(0, weight=1)
                editor_status_var = tk.StringVar(value="Ready")
                cursor_var = tk.StringVar(value="Byte inspector: select a byte")
                ttk.Label(
                    status_line,
                    textvariable=editor_status_var,
                    foreground="#555555",
                    anchor="w",
                ).grid(row=0, column=0, sticky="ew")
                ttk.Label(
                    status_line,
                    textvariable=cursor_var,
                    foreground="#555555",
                    anchor="w",
                    justify="left",
                    wraplength=420,
                ).grid(row=1, column=0, sticky="ew", pady=(2, 0))
                ttk.Separator(panel).grid(row=5, column=0, sticky="ew", pady=(4, 4))
            else:
                editor_status_var = None
                cursor_var = None
                ttk.Separator(panel).grid(row=4, column=0, sticky="ew", pady=(5, 4))

            text_frame = ttk.Frame(panel)
            text_frame.grid(row=text_row, column=0, sticky="nsew")
            text_frame.rowconfigure(1, weight=1)
            text_frame.columnconfigure(0, weight=1)
            legend_text = (
                "16 bytes/line  |  Hex (8 bits/byte)  |  ASCII preview"
                if source == "device"
                else "16 bytes/line  |  Hex (8 bits/byte)  |  ASCII preview — edit either side"
            )
            ttk.Label(
                text_frame,
                text=legend_text,
                foreground="#666666",
                anchor="w",
            ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
            view = tk.Text(
                text_frame,
                font=("Consolas", 9),
                wrap="none",
                width=42,
                height=8,
                undo=source == "file",
                maxundo=200,
                exportselection=False,
            )
            yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=view.yview)
            xscroll = ttk.Scrollbar(text_frame, orient="horizontal", command=view.xview)
            view.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
            view.grid(row=1, column=0, sticky="nsew")
            yscroll.grid(row=1, column=1, sticky="ns")
            xscroll.grid(row=2, column=0, sticky="ew")
            if source == "device":
                self._set_text(view, initial_text)
            else:
                view.tag_configure("paired_byte", background="#DDEBFA")

            setattr(self, f"{memory}_{source}_panel", panel)
            setattr(self, f"{memory}_{source}_controls_frame", controls)
            setattr(self, f"{memory}_{source}_capacity_var", status_var)
            setattr(self, f"{memory}_{source}_capacity_bar", bar)
            setattr(self, f"{memory}_{source}_details_var", detail_var)
            setattr(self, f"{memory}_{source}_view_text", view)
            setattr(self, f"{memory}_{source}_view_page", panel)

            if source == "file":
                setattr(self, f"{memory}_editor_text", view)
                setattr(self, f"{memory}_editor_status_var", editor_status_var)
                setattr(self, f"{memory}_editor_cursor_var", cursor_var)
                view.bind("<KeyPress>", lambda event, name=memory: self._editor_keypress(name, event))
                view.bind("<ButtonRelease-1>", lambda _event, name=memory: self.after_idle(lambda: self._highlight_editor_byte(name)))
                for key in ("Left", "Right", "Up", "Down", "Prior", "Next", "Home", "End"):
                    view.bind(f"<KeyRelease-{key}>", lambda _event, name=memory: self.after_idle(lambda: self._highlight_editor_byte(name)))
                view.bind("<<Modified>>", lambda _event, name=memory: self._freeform_editor_modified(name))

        setattr(self, f"{memory}_view_notebook", None)
        setattr(self, f"{memory}_capacity_var", getattr(self, f"{memory}_device_capacity_var"))
        setattr(self, f"{memory}_capacity_bar", getattr(self, f"{memory}_device_capacity_bar"))
        self._load_memory_into_editor(memory, bytes([0xFF]) * capacity, source="Blank 0xFF buffer")

    def _select_memory_view(self, memory: str, source: str) -> None:
        view = getattr(self, f"{memory}_{source}_view_text")
        try:
            view.see("1.0")
            view.focus_set()
        except tk.TclError:
            pass

    @staticmethod
    def _compact_memory_range(stats: MemoryStats) -> str:
        if stats.first_programmed is None or stats.last_programmed is None:
            return "none (blank)"
        return f"0x{stats.first_programmed:04X}-0x{stats.last_programmed:04X}"

    def _update_device_memory_overview(self, memory: str, stats: MemoryStats, source: str) -> None:
        bar = getattr(self, f"{memory}_device_capacity_bar")
        status_var = getattr(self, f"{memory}_device_capacity_var")
        detail_var = getattr(self, f"{memory}_device_details_var")
        bar.configure(maximum=max(1, stats.capacity), value=stats.programmed_bytes)
        page_size = FLASH_PAGE_SIZE if memory == "flash" else EEPROM_PAGE_SIZE
        status_var.set(
            f"{stats.programmed_bytes:,} / {stats.capacity:,} bytes programmed "
            f"({stats.used_percent:.1f}%)"
        )
        detail_var.set(
            f"{source}: capacity {format_capacity(stats.capacity)}; "
            f"{format_bytes(stats.erased_bytes)} free; "
            f"address range {self._compact_memory_range(stats)}; "
            f"{format_pages(stats.programmed_pages, stats.total_pages, page_size)}; "
            f"SHA-256 {stats.sha256[:12]}…"
        )

    def _update_selected_file_overview(
        self,
        memory: str,
        capacity: int,
        page_size: int,
        path: Path,
        info: object,
        stats: MemoryStats,
    ) -> None:
        bar = getattr(self, f"{memory}_file_capacity_bar")
        status_var = getattr(self, f"{memory}_file_capacity_var")
        detail_var = getattr(self, f"{memory}_file_details_var")
        occupied = int(getattr(info, "occupied_bytes"))
        minimum = int(getattr(info, "minimum_address"))
        maximum = int(getattr(info, "maximum_address"))
        file_type = str(getattr(info, "file_type"))
        sha256 = str(getattr(info, "sha256"))
        bar.configure(maximum=max(1, capacity), value=min(occupied, capacity))
        status_var.set(f"{occupied:,} / {capacity:,} bytes addressed ({occupied / capacity * 100:.1f}%)")
        detail_var.set(
            f"{path.name} — {file_type}; target capacity {format_capacity(capacity)}; "
            f"byte addresses 0x{minimum:04X}-0x{maximum:04X}; "
            f"{format_bytes(stats.programmed_bytes)} not equal to 0xFF; "
            f"{format_pages(stats.programmed_pages, stats.total_pages, page_size)}; "
            f"SHA-256 {sha256[:12]}…"
        )

    def _save_operation_options(self) -> None:
        if self.operation_full_readback_var.get() and not self.operation_verify_var.get():
            # Full readback still verifies the result, so this combination is valid.
            pass
        self.settings.values.update({
            "automatic_backup": bool(self.operation_backup_var.get()),
            "verify_after_write": bool(self.operation_verify_var.get()),
            "full_readback_verification": bool(self.operation_full_readback_var.get()),
            "isp_speed_mode": self.isp_speed_var.get(),
        })
        self.settings.save()
        self.service._apply_isp_speed()
        self._update_operation_options_summary()
        if hasattr(self, "project_summary_text"):
            self._project_refresh_summary()

    def _update_operation_options_summary(self) -> None:
        try:
            period = self.service._effective_bitclock_us()
            speed = f"{self.isp_speed_var.get()} → -B {period:g} µs"
        except Exception:
            speed = self.isp_speed_var.get()
        backup = "Backup ON" if self.operation_backup_var.get() else "Backup OFF"
        verify = "Write check ON" if self.operation_verify_var.get() else "Write check OFF"
        readback = "Read-again check ON" if self.operation_full_readback_var.get() else "Read-again check OFF"
        warning = ""
        if not self.operation_verify_var.get() and not self.operation_full_readback_var.get():
            warning = " — WARNING: write will not be verified"
        self.operation_options_summary_var.set(f"{backup}  •  {verify}  •  {readback}  •  ISP {speed}{warning}")

    def _show_isp_speed_help(self) -> None:
        messagebox.showinfo(
            "ISP speed",
            "Automatic is recommended. ATtiny85 Explorer uses the detected chip clock to choose a safe programmer speed and retries detection more slowly when needed.\n\n"
            "Fast (1 µs) is useful for a known 8–16 MHz target with short, reliable wiring.\n\n"
            "Compatible (10 µs) is suitable for factory 1 MHz parts and most troubleshooting.\n\n"
            "Slow (40 µs) is intended for very slow clocks such as 128 kHz. It will make reads and writes noticeably slower.",
            parent=self,
        )

    def _show_verification_help(self) -> None:
        messagebox.showinfo(
            "Programming checks",
            "Verify during write: AVRDUDE checks the bytes it just programmed before the command finishes. Keep this enabled for normal use.\n\n"
            "Second full readback: ATtiny85 Explorer performs another complete memory read, compares the final resolved image byte-for-byte, and refreshes the DEVICE panel. This is the slowest and strongest optional check.\n\n"
            "Compare chip ↔ buffer: a separate read-only action. It never writes the chip and compares the entire current editor buffer to the connected memory.\n\n"
            "Complete backup: saves flash, EEPROM, fuses, and lock bits before programming. It adds time because the chip must be read first.",
            parent=self,
        )

    def _set_operation_profile(self, profile: str) -> None:
        if profile == "recommended":
            self.operation_backup_var.set(True)
            self.operation_verify_var.set(True)
            self.operation_full_readback_var.set(False)
            self.isp_speed_var.set("Automatic")
        elif profile == "maximum":
            self.operation_backup_var.set(True)
            self.operation_verify_var.set(True)
            self.operation_full_readback_var.set(True)
            self.isp_speed_var.set("Automatic")
        elif profile == "faster":
            self.operation_backup_var.set(False)
            self.operation_verify_var.set(True)
            self.operation_full_readback_var.set(False)
            self.isp_speed_var.set("Automatic")
        self._save_operation_options()

    def _build_operation_options(
        self,
        parent: tk.Misc,
        memory: str,
        row: int,
        column: int = 0,
        padx: Tuple[int, int] = (0, 0),
    ) -> None:
        frame = ttk.LabelFrame(parent, text="Programming options", padding=4)
        frame.grid(row=row, column=column, sticky="ew", padx=padx, pady=(4, 0))
        frame.columnconfigure(0, weight=1)
        setattr(self, f"{memory}_operation_options_frame", frame)

        checks = ttk.Frame(frame)
        checks.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(
            checks,
            text="Complete backup first",
            variable=self.operation_backup_var,
            command=self._save_operation_options,
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Checkbutton(
            checks,
            text="Verify during write",
            variable=self.operation_verify_var,
            command=self._save_operation_options,
        ).grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Checkbutton(
            checks,
            text="Second full readback",
            variable=self.operation_full_readback_var,
            command=self._save_operation_options,
        ).grid(row=0, column=2, sticky="w")

        lower = ttk.Frame(frame)
        lower.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        lower.columnconfigure(0, weight=1)
        if memory == "flash":
            ttk.Checkbutton(
                lower,
                text="Restore EEPROM if flash erase clears it",
                variable=self.flash_restore_eeprom_var,
                command=lambda: self.settings.set(
                    "restore_eeprom_after_flash_write",
                    bool(self.flash_restore_eeprom_var.get()),
                ),
            ).grid(row=0, column=0, sticky="w")

        profiles = ttk.Frame(lower)
        profiles.grid(row=0, column=1, sticky="e")
        ttk.Button(profiles, text="Safe", width=6, command=lambda: self._set_operation_profile("recommended")).grid(row=0, column=0, padx=(0, 3))
        ttk.Button(profiles, text="Thorough", width=8, command=lambda: self._set_operation_profile("maximum")).grid(row=0, column=1, padx=(0, 3))
        ttk.Button(profiles, text="Fast", width=6, command=lambda: self._set_operation_profile("faster")).grid(row=0, column=2, padx=(0, 3))
        ttk.Button(profiles, text="?", width=3, command=self._show_verification_help).grid(row=0, column=3)

        self._update_operation_options_summary()

    def _build_memory_action_groups(
        self,
        parent: tk.Misc,
        memory: str,
        row: int,
        column: int = 0,
        padx: Tuple[int, int] = (0, 0),
    ) -> None:
        actions = ttk.Frame(parent)
        actions.grid(row=row, column=column, sticky="nsew", padx=padx)
        actions.columnconfigure(0, weight=1)

        device = ttk.LabelFrame(actions, text="DEVICE actions", padding=3)
        device.grid(row=0, column=0, sticky="ew")
        selected = ttk.LabelFrame(actions, text="SELECTED FILE actions", padding=3)
        selected.grid(row=1, column=0, sticky="ew", pady=(5, 0))

        if memory == "flash":
            device_buttons = (
                ("Read", self._read_flash),
                ("Save copy…", self._read_flash_to_file),
                ("Blank?", lambda: self._blank_check("flash")),
                ("Erase + reread…", self._erase_flash),
            )
            file_buttons = (
                ("Program file", self._write_flash),
                ("Compare chip ↔ file", self._verify_flash),
            )
        else:
            device_buttons = (
                ("Read", self._read_eeprom),
                ("Save copy…", self._read_eeprom_to_file),
                ("Blank?", lambda: self._blank_check("eeprom")),
                ("Erase + reread…", self._erase_eeprom),
            )
            file_buttons = (
                ("Program file", self._write_eeprom),
                ("Compare chip ↔ file", self._verify_eeprom),
            )
        for button_column, (label, command) in enumerate(device_buttons):
            ttk.Button(device, text=label, command=command).grid(row=0, column=button_column, padx=(0, 4))
        for button_column, (label, command) in enumerate(file_buttons):
            ttk.Button(selected, text=label, command=command).grid(row=0, column=button_column, padx=(0, 4))

    def _mark_device_readout_stale(self, memory: str) -> None:
        detail_var = getattr(self, f"{memory}_device_details_var")
        previous = detail_var.get()
        if "STALE" not in previous:
            detail_var.set(
                "STALE after programming — the selected target was written, but a second full device readback was not requested. "
                "Click Read device, or enable Read chip again after write, to refresh this side."
            )

    def _comparison_completed(self, memory: str, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if isinstance(outcome, OperationOutcome):
            readback = outcome.extra.get("readback", b"")
            if isinstance(readback, bytes) and readback:
                self._display_memory_stats(memory, readback, "comparison readback", True)
                first = outcome.extra.get("first_mismatch")
                if isinstance(first, int):
                    editor = getattr(self, f"{memory}_editor_text")
                    index = self._editor_index_for_byte(first, "ascii")
                    editor.mark_set("insert", index)
                    editor.see(index)
                    self._highlight_editor_byte(memory)
                self._select_memory_view(memory, "device")
            self._show_outcome(outcome)

    def _build_flash_tab(self) -> None:
        self.flash_path_var = tk.StringVar()
        self.flash_offset_var = tk.StringVar(value="0x0000")
        self.flash_merge_var = tk.BooleanVar(value=False)
        self.flash_preserve_var = tk.BooleanVar(value=False)
        self.flash_preserve_start_var = tk.StringVar(value="0x1C00")
        self.flash_restore_eeprom_var = tk.BooleanVar(value=bool(self.settings.get("restore_eeprom_after_flash_write", True)))
        self.flash_erase_preserve_eeprom_var = tk.BooleanVar(value=True)
        self.flash_editor_format_var = tk.StringVar(value="Addressed hex")
        self.flash_editor_current_format = "Addressed hex"

        self._build_memory_workspace(self.flash_tab, "flash")

        device_controls = self.flash_device_controls_frame
        ttk.Label(device_controls, text="Device actions", style="Status.TLabel").grid(row=0, column=0, sticky="w")
        device_buttons = ttk.Frame(device_controls)
        device_buttons.grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Button(device_buttons, text="Read device", command=self._read_flash).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(device_buttons, text="Read + save…", command=self._read_flash_to_file).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(device_buttons, text="Blank check", command=lambda: self._blank_check("flash")).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(device_buttons, text="Erase + reread…", command=self._erase_flash).grid(row=0, column=3, padx=(0, 8))
        ttk.Checkbutton(device_buttons, text="Preserve EEPROM", variable=self.flash_erase_preserve_eeprom_var).grid(row=0, column=4, sticky="w")

        file_controls = self.flash_file_controls_frame
        picker = ttk.Frame(file_controls)
        picker.grid(row=0, column=0, sticky="ew")
        picker.columnconfigure(1, weight=1)
        ttk.Label(picker, text="File:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        path_entry = ttk.Entry(picker, textvariable=self.flash_path_var, width=28)
        path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        path_entry.bind("<Return>", lambda _event: self._inspect_flash())
        ttk.Button(picker, text="Browse…", command=self._browse_flash).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(picker, text="Reload", command=self._inspect_flash).grid(row=0, column=3)

        buffer_tools = ttk.Frame(file_controls)
        buffer_tools.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(buffer_tools, text="Buffer tools", style="Status.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(buffer_tools, text="Copy device", command=lambda: self._load_device_into_editor("flash")).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(buffer_tools, text="New blank", command=lambda: self._new_blank_editor("flash")).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(buffer_tools, text="Undo", command=lambda: self._undo_editor("flash")).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(buffer_tools, text="Redo", command=lambda: self._redo_editor("flash")).grid(row=0, column=4, padx=(0, 4))
        ttk.Button(buffer_tools, text="Save…", command=lambda: self._save_editor("flash")).grid(row=0, column=5)

        self._build_operation_options(file_controls, "flash", 2)

        file_actions = ttk.Frame(file_controls)
        file_actions.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(file_actions, text="Editor actions", style="Status.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(file_actions, text="Program buffer", command=lambda: self._upload_editor("flash")).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(file_actions, text="Compare chip ↔ buffer", command=lambda: self._compare_editor_buffer("flash")).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(file_actions, text="Check buffer", command=lambda: self._validate_editor("flash")).grid(row=0, column=3)
        self.flash_retry_button = ttk.Button(
            file_actions,
            text="Retry same buffer",
            command=lambda: self._retry_memory_write("flash"),
        )
        self.flash_retry_button.grid(row=0, column=4, padx=(8, 0))
        self.flash_retry_button.grid_remove()

        self.flash_advanced_frame = ttk.LabelFrame(file_controls, text="Advanced buffer and file options", padding=4)
        self.flash_advanced_frame.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(self.flash_advanced_frame, text="View format:").grid(row=0, column=0, sticky="w")
        self.flash_editor_format_combo = ttk.Combobox(
            self.flash_advanced_frame,
            textvariable=self.flash_editor_format_var,
            state="readonly",
            width=14,
            values=("Addressed hex", "Intel HEX", "Raw hex bytes"),
        )
        self.flash_editor_format_combo.grid(row=0, column=1, padx=(4, 4), sticky="w")
        self.flash_editor_format_combo.bind("<<ComboboxSelected>>", lambda _event: self._convert_editor_format("flash"))
        ttk.Button(self.flash_advanced_frame, text="Convert", command=lambda: self._convert_editor_format("flash")).grid(row=0, column=2, padx=(0, 12))
        ttk.Label(self.flash_advanced_frame, text="BIN offset:").grid(row=0, column=3, sticky="w")
        offset_entry = ttk.Entry(self.flash_advanced_frame, textvariable=self.flash_offset_var, width=10)
        offset_entry.grid(row=0, column=4, sticky="w", padx=(4, 12))
        offset_entry.bind("<Return>", lambda _event: self._inspect_flash())
        ttk.Checkbutton(self.flash_advanced_frame, text="Merge unspecified bytes with current flash", variable=self.flash_merge_var).grid(row=0, column=5, sticky="w")
        ttk.Checkbutton(self.flash_advanced_frame, text="Preserve upper flash from", variable=self.flash_preserve_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Entry(self.flash_advanced_frame, textvariable=self.flash_preserve_start_var, width=10).grid(row=1, column=2, sticky="w", pady=(3, 0))

    def _browse_flash(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Select flash image", filetypes=[("Firmware images", "*.hex *.ihx *.bin"), ("All files", "*.*")])
        if path:
            self.flash_path_var.set(path)
            if Path(path).suffix.lower() not in (".hex", ".ihx"):
                self.flash_merge_var.set(True)
            self._inspect_flash()

    def _inspect_flash(self, silent: bool = False) -> bool:
        raw_path = self.flash_path_var.get().strip()
        if not raw_path:
            return False
        if self.editor_dirty.get("flash", False):
            if silent:
                return False
            if not messagebox.askyesno(
                "Replace flash editor buffer",
                "Loading this file will replace unsaved edits in the flash buffer. Continue?",
                parent=self,
            ):
                self.flash_path_var.set(self.editor_loaded_paths.get("flash", ""))
                return False
        try:
            selected_path = Path(raw_path)
            offset = parse_number(self.flash_offset_var.get())
            info = inspect_image(selected_path, offset=offset)
            image, _payload = load_image(selected_path, FLASH_SIZE, offset=offset)
            self.flash_editor_format_var.set("Addressed hex")
            source = (
                f"File: {selected_path.name} — {info.file_type}; "
                f"addresses 0x{info.minimum_address:04X}-0x{info.maximum_address:04X}"
            )
            self._load_memory_into_editor("flash", image, source=source)
            self.editor_loaded_paths["flash"] = str(selected_path)
            self.status_var.set(f"Loaded {selected_path.name} into the flash editor buffer")
            self._select_memory_view("flash", "file")
            return True
        except Exception as exc:
            self.flash_file_capacity_var.set("File could not be loaded")
            self.flash_file_details_var.set(f"{exc} Existing editor buffer was preserved.")
            self._editor_status("flash", f"File load failed: {exc}")
            if not silent:
                messagebox.showerror("Flash file load", str(exc), parent=self)
            return False

    def _read_flash(self) -> None:
        self._run_task("Reading flash", lambda: self.service.read_memory("flash"), lambda result: self._memory_read_completed("flash", result))

    def _read_flash_to_file(self) -> None:
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Save flash dump",
            defaultextension=".hex",
            filetypes=[("Intel HEX", "*.hex"), ("Raw binary", "*.bin"), ("All files", "*.*")],
        )
        if destination:
            self._run_task(
                "Reading and saving flash",
                lambda: self.service.read_flash_to_file(Path(destination)),
                lambda result: self._memory_read_completed("flash", result),
            )

    def _memory_read_completed(self, memory: str, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if not isinstance(outcome, OperationOutcome) or not outcome.success:
            if isinstance(outcome, OperationOutcome):
                self._show_outcome(outcome)
            return
        data = outcome.extra.get("data", b"")
        if not isinstance(data, bytes) or not data:
            messagebox.showerror("Memory read", "The backend returned no memory data.", parent=self)
            return
        self._display_memory_stats(memory, data, "device readback", True)
        self._editor_status(
            memory,
            "Device readback updated on the left. The editor buffer was not changed; click Copy device to work from this readout.",
        )
        self._append_log(f"{outcome.title}: {outcome.detail}")
        self.notebook.select(self.flash_tab if memory == "flash" else self.eeprom_tab)
        self._select_memory_view(memory, "device")
        self.status_var.set(f"{memory.title()} read, analyzed, and displayed")

    def _editor_controls(self, memory: str):
        if memory == "flash":
            return (
                self.flash_editor_text,
                self.flash_editor_format_var,
                self.flash_editor_current_format,
                FLASH_SIZE,
                FLASH_PAGE_SIZE,
                self.flash_path_var,
                self.flash_offset_var,
            )
        return (
            self.eeprom_editor_text,
            self.eeprom_editor_format_var,
            self.eeprom_editor_current_format,
            EEPROM_SIZE,
            EEPROM_PAGE_SIZE,
            self.eeprom_path_var,
            self.eeprom_offset_var,
        )

    def _set_editor_current_format(self, memory: str, value: str) -> None:
        if memory == "flash":
            self.flash_editor_current_format = value
        else:
            self.eeprom_editor_current_format = value

    def _editor_status(self, memory: str, text: str) -> None:
        variable = getattr(self, f"{memory}_editor_status_var", None)
        if variable is not None:
            variable.set(text)

    def _render_editor_buffer(self, memory: str, keep_index: Optional[str] = None) -> None:
        editor, _format_var, current_format, _capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        if current_format != "Addressed hex":
            return
        data = bytes(self.editor_images.get(memory, bytearray()))
        if not data:
            return
        index = keep_index or editor.index("insert")
        yview = editor.yview()
        editor.delete("1.0", "end")
        editor.insert("1.0", editor_text_from_memory(data, "Addressed hex"))
        editor.edit_reset()
        editor.edit_modified(False)
        try:
            editor.mark_set("insert", index)
            if yview:
                editor.yview_moveto(yview[0])
        except tk.TclError:
            editor.mark_set("insert", "1.56")
        self._highlight_editor_byte(memory)

    def _load_memory_into_editor(self, memory: str, data: bytes, source: str = "Loaded image") -> None:
        editor, format_var, _current, capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        if len(data) != capacity:
            raise ValueError(f"{memory.title()} image has {len(data)} bytes; expected {capacity}")
        self._set_retry_memory_write(memory, False)
        format_name = format_var.get()
        editor.configure(state="normal")
        self.editor_images[memory] = bytearray(data)
        self.editor_baselines[memory] = bytes(data)
        self.editor_sources[memory] = source
        self.editor_dirty[memory] = False
        self.editor_undo[memory].clear()
        self.editor_redo[memory].clear()
        editor.delete("1.0", "end")
        editor.insert("1.0", editor_text_from_memory(data, format_name))
        editor.edit_reset()
        editor.edit_modified(False)
        self._set_editor_current_format(memory, format_name)
        self._editor_status(
            memory,
            "Ready — edit hex or ASCII. Program uses this buffer.",
        )
        self._refresh_editor_overview(memory)
        if format_name == "Addressed hex":
            editor.mark_set("insert", "1.56")
            self._highlight_editor_byte(memory)

    def _load_selected_into_editor(self, memory: str) -> None:
        if memory == "flash":
            self._inspect_flash()
        else:
            self._inspect_eeprom()

    def _load_device_into_editor(self, memory: str) -> None:
        data = self.memory_images.get(memory)
        if not isinstance(data, bytes) or not data:
            messagebox.showerror("Copy device to editor", f"Read the device {memory} first.", parent=self)
            return
        if self.editor_dirty.get(memory, False) and not messagebox.askyesno(
            "Replace editor buffer",
            "The editor contains changes that have not been saved or programmed. Replace them with the current device readback?",
            parent=self,
        ):
            return
        path_var = self.flash_path_var if memory == "flash" else self.eeprom_path_var
        path_var.set("")
        self.editor_loaded_paths[memory] = ""
        format_var = self.flash_editor_format_var if memory == "flash" else self.eeprom_editor_format_var
        format_var.set("Addressed hex")
        self._load_memory_into_editor(memory, data, source="Copied from current device readback")
        self._select_memory_view(memory, "file")
        self.status_var.set(f"Copied current device {memory} into the editor buffer")

    def _refresh_editor_overview(self, memory: str) -> None:
        data = bytes(self.editor_images.get(memory, bytearray()))
        if not data:
            return
        page_size = FLASH_PAGE_SIZE if memory == "flash" else EEPROM_PAGE_SIZE
        stats = analyze_memory(data, page_size)
        bar = getattr(self, f"{memory}_file_capacity_bar")
        status_var = getattr(self, f"{memory}_file_capacity_var")
        detail_var = getattr(self, f"{memory}_file_details_var")
        bar.configure(maximum=max(1, stats.capacity), value=stats.programmed_bytes)
        status_var.set(f"{stats.programmed_bytes:,} / {stats.capacity:,} bytes programmed ({stats.used_percent:.1f}%)")
        source = self.editor_sources.get(memory, "Editor buffer")
        baseline = self.editor_baselines.get(memory, b"")
        changed = sum(1 for old, new in zip(baseline, data) if old != new) if len(baseline) == len(data) else 0
        edit_state = f"MODIFIED: {format_bytes(changed)} changed" if self.editor_dirty.get(memory, False) else "ready"
        detail_var.set(
            f"{source}; {edit_state}; capacity {format_capacity(stats.capacity)}; "
            f"byte address range {self._compact_memory_range(stats)}; "
            f"{format_pages(stats.programmed_pages, stats.total_pages, page_size)}; "
            f"SHA-256 {stats.sha256[:12]}…"
        )

    def _new_blank_editor(self, memory: str) -> None:
        if self.editor_dirty.get(memory, False) and not messagebox.askyesno(
            "Replace editor buffer",
            "Discard the unsaved changes and start a new all-0xFF buffer?",
            parent=self,
        ):
            return
        capacity = FLASH_SIZE if memory == "flash" else EEPROM_SIZE
        path_var = self.flash_path_var if memory == "flash" else self.eeprom_path_var
        format_var = self.flash_editor_format_var if memory == "flash" else self.eeprom_editor_format_var
        path_var.set("")
        self.editor_loaded_paths[memory] = ""
        format_var.set("Addressed hex")
        self._load_memory_into_editor(memory, bytes([0xFF]) * capacity, source="Blank 0xFF buffer")
        self._select_memory_view(memory, "file")
        self.status_var.set(f"New blank {memory} editor buffer")

    def _compare_editor_buffer(self, memory: str) -> None:
        try:
            data = self._parse_editor(memory)
        except Exception as exc:
            messagebox.showerror("Compare chip to buffer", str(exc), parent=self)
            return
        self._run_task(
            f"Comparing {memory} chip ↔ editor buffer",
            lambda: self.service.verify_data(memory, data),
            lambda result: self._comparison_completed(memory, result),
        )

    def _set_retry_memory_write(self, memory: str, visible: bool) -> None:
        """Show a retry action only after a recoverable partial/timeout write."""
        self.retry_write_ready[memory] = bool(visible)
        button = getattr(self, f"{memory}_retry_button", None)
        if button is None:
            return
        if visible:
            button.grid()
        else:
            button.grid_remove()

    def _retry_memory_write(self, memory: str) -> None:
        if not self.retry_write_ready.get(memory, False):
            return
        self._upload_editor(memory)

    def _parse_editor_details(self, memory: str) -> Tuple[bytes, int]:
        editor, _format_var, current_format, capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        if current_format == "Addressed hex" and memory in self.editor_images:
            data = bytes(self.editor_images[memory])
            if len(data) != capacity:
                raise ValueError(f"{memory.title()} editor buffer has {len(data)} bytes; expected {capacity}")
            return data, capacity
        text = editor.get("1.0", "end-1c")
        data, addressed = memory_from_editor_text(text, capacity, current_format)
        self.editor_images[memory] = bytearray(data)
        self._update_editor_dirty_status(memory)
        return data, addressed

    def _parse_editor(self, memory: str) -> bytes:
        data, _addressed = self._parse_editor_details(memory)
        return data

    def _convert_editor_format(self, memory: str) -> None:
        editor, format_var, current_format, _capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        target_format = format_var.get()
        if target_format == current_format:
            return
        try:
            data = self._parse_editor(memory)
            self.editor_images[memory] = bytearray(data)
            editor.delete("1.0", "end")
            editor.insert("1.0", editor_text_from_memory(data, target_format))
            editor.edit_reset()
            editor.edit_modified(False)
            self._set_editor_current_format(memory, target_format)
            self._update_editor_dirty_status(memory)
            if target_format == "Addressed hex":
                editor.mark_set("insert", "1.56")
                self._highlight_editor_byte(memory)
            self.status_var.set(f"{memory.title()} editor converted to {target_format}")
        except Exception as exc:
            format_var.set(current_format)
            messagebox.showerror("Format conversion", f"Fix the current editor data before converting formats.\n\n{exc}", parent=self)

    def _editor_position(self, memory: str, index: Optional[str] = None) -> Optional[Tuple[int, str, int]]:
        editor, _format_var, current_format, capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        if current_format != "Addressed hex":
            return None
        line_text, column_text = editor.index(index or "insert").split(".", 1)
        line = int(line_text) - 1
        column = int(column_text)
        if line < 0:
            return None
        byte_index = -1
        region = ""
        nibble = 0
        if 56 <= column < 72:
            byte_index = line * 16 + (column - 56)
            region = "ascii"
        elif 6 <= column <= 52:
            relative = column - 6
            within = relative % 3
            if within in (0, 1):
                byte_index = line * 16 + relative // 3
                region = "hex"
                nibble = within
        if byte_index < 0 or byte_index >= capacity:
            return None
        return byte_index, region, nibble

    def _editor_index_for_byte(self, byte_index: int, region: str = "ascii", nibble: int = 0) -> str:
        line = byte_index // 16 + 1
        within = byte_index % 16
        column = 56 + within if region == "ascii" else 6 + within * 3 + nibble
        return f"{line}.{column}"

    def _highlight_editor_byte(self, memory: str) -> None:
        editor, _format_var, current_format, _capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        editor.tag_remove("paired_byte", "1.0", "end")
        cursor_var = getattr(self, f"{memory}_editor_cursor_var", None)
        if current_format != "Addressed hex":
            if cursor_var is not None:
                cursor_var.set(f"Byte inspector: {current_format} text view")
            return
        position = self._editor_position(memory)
        if position is None:
            if cursor_var is not None:
                cursor_var.set("Byte inspector: select a byte")
            return
        byte_index, region, _nibble = position
        line = byte_index // 16 + 1
        within = byte_index % 16
        hex_column = 6 + within * 3
        ascii_column = 56 + within
        editor.tag_add("paired_byte", f"{line}.{hex_column}", f"{line}.{hex_column + 2}")
        editor.tag_add("paired_byte", f"{line}.{ascii_column}", f"{line}.{ascii_column + 1}")
        data = self.editor_images.get(memory, bytearray())
        if cursor_var is not None and byte_index < len(data):
            value = data[byte_index]
            character = chr(value) if 32 <= value <= 126 else "."
            cursor_var.set(
                f"Byte inspector — Addr 0x{byte_index:04X} | Hex 0x{value:02X} | "
                f"Bin {value:08b} | Dec {value:d} | ASCII '{character}' | edit {region}"
            )

    def _push_editor_undo(self, memory: str) -> None:
        current = bytes(self.editor_images.get(memory, bytearray()))
        stack = self.editor_undo[memory]
        if not stack or stack[-1] != current:
            stack.append(current)
            if len(stack) > 200:
                del stack[0]
        self.editor_redo[memory].clear()

    def _undo_editor(self, memory: str) -> None:
        if not self.editor_undo[memory]:
            self.bell()
            return
        self._set_retry_memory_write(memory, False)
        current = bytes(self.editor_images.get(memory, bytearray()))
        previous = self.editor_undo[memory].pop()
        self.editor_redo[memory].append(current)
        self.editor_images[memory] = bytearray(previous)
        self._render_editor_buffer(memory)
        self._update_editor_dirty_status(memory)

    def _redo_editor(self, memory: str) -> None:
        if not self.editor_redo[memory]:
            self.bell()
            return
        self._set_retry_memory_write(memory, False)
        current = bytes(self.editor_images.get(memory, bytearray()))
        following = self.editor_redo[memory].pop()
        self.editor_undo[memory].append(current)
        self.editor_images[memory] = bytearray(following)
        self._render_editor_buffer(memory)
        self._update_editor_dirty_status(memory)

    def _update_editor_dirty_status(self, memory: str) -> None:
        data = bytes(self.editor_images.get(memory, bytearray()))
        baseline = self.editor_baselines.get(memory, b"")
        dirty = bool(data) and data != baseline
        self.editor_dirty[memory] = dirty
        source = self.editor_sources.get(memory, "Editor buffer")
        if not dirty:
            self._editor_status(memory, "Ready — no unsaved edits.")
        else:
            changed = sum(1 for old, new in zip(baseline, data) if old != new) if len(baseline) == len(data) else len(data)
            self._editor_status(
                memory,
                f"MODIFIED — {changed} byte{'s' if changed != 1 else ''} changed. Save, compare, or program.",
            )
        self._refresh_editor_overview(memory)

    def _set_editor_bytes(self, memory: str, updates: List[Tuple[int, int]], next_index: Optional[str] = None) -> None:
        if not updates:
            return
        self._set_retry_memory_write(memory, False)
        self._push_editor_undo(memory)
        buffer = self.editor_images[memory]
        for address, value in updates:
            if 0 <= address < len(buffer):
                buffer[address] = value & 0xFF
        self._render_editor_buffer(memory, keep_index=next_index)
        if next_index:
            editor = getattr(self, f"{memory}_editor_text")
            editor.mark_set("insert", next_index)
            editor.see(next_index)
        self._update_editor_dirty_status(memory)

    def _paste_into_editor(self, memory: str) -> str:
        position = self._editor_position(memory)
        if position is None:
            self.bell()
            return "break"
        byte_index, region, _nibble = position
        try:
            clipboard = self.clipboard_get()
        except tk.TclError:
            return "break"
        updates: List[Tuple[int, int]] = []
        capacity = FLASH_SIZE if memory == "flash" else EEPROM_SIZE
        current = byte_index
        if region == "ascii":
            normalized = clipboard.replace("\r\n", "\n").replace("\r", "\n")
            if "\n" in normalized:
                start_row = byte_index // 16
                start_column = byte_index % 16
                lines = normalized.split("\n")
                for row_offset, line in enumerate(lines):
                    current = (start_row + row_offset) * 16 + start_column
                    for character in line:
                        if current >= capacity:
                            break
                        try:
                            encoded = character.encode("latin-1")
                        except UnicodeEncodeError:
                            continue
                        updates.append((current, encoded[0]))
                        current += 1
                    if current >= capacity:
                        break
                final_row = min(start_row + max(0, len(lines) - 1), (capacity - 1) // 16)
                final_column = min(start_column + len(lines[-1]), 15)
                current = final_row * 16 + final_column
            else:
                current = byte_index
                for character in normalized:
                    if current >= capacity:
                        break
                    try:
                        encoded = character.encode("latin-1")
                    except UnicodeEncodeError:
                        continue
                    updates.append((current, encoded[0]))
                    current += 1
            next_index = self._editor_index_for_byte(min(current, capacity - 1), "ascii")
        else:
            tokens = re.findall(r"(?i)(?:0x)?([0-9a-f]{2})(?![0-9a-f])", clipboard)
            if not tokens:
                compact = re.sub(r"[^0-9A-Fa-f]", "", clipboard)
                if len(compact) >= 2 and len(compact) % 2 == 0:
                    tokens = [compact[index:index + 2] for index in range(0, len(compact), 2)]
            for token in tokens:
                if current >= capacity:
                    break
                updates.append((current, int(token, 16)))
                current += 1
            next_index = self._editor_index_for_byte(min(current, capacity - 1), "hex")
        self._set_editor_bytes(memory, updates, next_index=next_index)
        return "break"

    def _editor_keypress(self, memory: str, event: tk.Event) -> Optional[str]:
        _editor, _format_var, current_format, capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        if current_format != "Addressed hex":
            return None
        control = bool(int(getattr(event, "state", 0)) & 0x4)
        keysym = str(getattr(event, "keysym", ""))
        if control and keysym.lower() == "v":
            return self._paste_into_editor(memory)
        if control and keysym.lower() == "z":
            self._undo_editor(memory)
            return "break"
        if control and keysym.lower() == "y":
            self._redo_editor(memory)
            return "break"
        if control and keysym.lower() in ("c", "a", "f"):
            return None
        if control:
            return "break"

        position = self._editor_position(memory)
        if keysym in ("Left", "Right", "Up", "Down", "Prior", "Next", "Home", "End"):
            return None
        if position is None:
            if getattr(event, "char", ""):
                self.bell()
                return "break"
            return None
        byte_index, region, nibble = position

        if keysym in ("Delete", "BackSpace"):
            target = byte_index
            if keysym == "BackSpace" and target > 0:
                target -= 1
            next_index = self._editor_index_for_byte(target, region, 0)
            self._set_editor_bytes(memory, [(target, 0xFF)], next_index=next_index)
            return "break"

        character = str(getattr(event, "char", ""))
        if not character:
            return None
        if region == "ascii":
            try:
                encoded = character.encode("latin-1")
            except UnicodeEncodeError:
                self.bell()
                return "break"
            if len(encoded) != 1:
                return "break"
            next_byte = min(byte_index + 1, capacity - 1)
            next_index = self._editor_index_for_byte(next_byte, "ascii")
            self._set_editor_bytes(memory, [(byte_index, encoded[0])], next_index=next_index)
            return "break"
        if region == "hex" and character in "0123456789abcdefABCDEF":
            current_value = self.editor_images[memory][byte_index]
            digit = int(character, 16)
            new_value = ((digit << 4) | (current_value & 0x0F)) if nibble == 0 else ((current_value & 0xF0) | digit)
            if nibble == 0:
                next_index = self._editor_index_for_byte(byte_index, "hex", 1)
            else:
                next_byte = min(byte_index + 1, capacity - 1)
                next_index = self._editor_index_for_byte(next_byte, "hex", 0)
            self._set_editor_bytes(memory, [(byte_index, new_value)], next_index=next_index)
            return "break"
        self.bell()
        return "break"

    def _freeform_editor_modified(self, memory: str) -> None:
        editor, _format_var, current_format, _capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
        if not editor.edit_modified():
            return
        editor.edit_modified(False)
        if current_format != "Addressed hex":
            self.editor_dirty[memory] = True
            self._set_retry_memory_write(memory, False)
            self._editor_status(memory, "MODIFIED TEXT — Check buffer before programming.")

    def _validate_editor(self, memory: str) -> None:
        try:
            data, addressed = self._parse_editor_details(memory)
            self.editor_images[memory] = bytearray(data)
            self._update_editor_dirty_status(memory)
            stats = self._display_memory_stats(memory, data, "edited image", False)
            original = self.memory_original_images.get(memory)
            changes = sum(1 for old, new in zip(original, data) if old != new) if original is not None else None
            change_text = "Current device was not read in this session." if changes is None else f"Bytes changed from last device read: {changes:,} bytes."
            sparse_text = ""
            if addressed < len(data):
                sparse_text = f"\nOnly {addressed:,} byte addresses were explicitly supplied; every unspecified byte becomes 0xFF."
            messagebox.showinfo(
                f"{memory.title()} editor valid",
                f"The editor resolves to a complete {len(data)}-byte image.\n"
                f"Explicitly addressed bytes: {addressed:,} bytes.\n"
                f"Programmed bytes: {stats.programmed_bytes:,} bytes.\n{change_text}{sparse_text}",
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror(f"{memory.title()} editor error", str(exc), parent=self)

    def _save_editor(self, memory: str) -> None:
        try:
            data = self._parse_editor(memory)
        except Exception as exc:
            messagebox.showerror("Save editor", str(exc), parent=self)
            return
        default_extension = ".hex" if memory == "flash" else ".bin"
        path_text = filedialog.asksaveasfilename(
            parent=self,
            title=f"Save {memory} editor buffer",
            defaultextension=default_extension,
            filetypes=[("Intel HEX", "*.hex"), ("Raw binary", "*.bin"), ("Editor text", "*.txt"), ("All files", "*.*")],
        )
        if not path_text:
            return
        output_path = Path(path_text)
        try:
            if output_path.suffix.lower() in (".hex", ".ihx"):
                write_intel_hex(bytes_to_memory(data), output_path)
            elif output_path.suffix.lower() == ".txt":
                editor, _format_var, _current, _capacity, _page, _path_var, _offset_var = self._editor_controls(memory)
                output_path.write_text(editor.get("1.0", "end-1c"), encoding="utf-8")
            else:
                output_path.write_bytes(data)
            path_var = self.flash_path_var if memory == "flash" else self.eeprom_path_var
            path_var.set(str(output_path))
            self.editor_loaded_paths[memory] = str(output_path)
            self.editor_baselines[memory] = bytes(data)
            self.editor_sources[memory] = f"Saved file: {output_path.name}"
            self._update_editor_dirty_status(memory)
            messagebox.showinfo("Editor buffer saved", f"Saved the complete {memory} buffer to:\n{output_path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Editor save failed", str(exc), parent=self)

    def _upload_editor(self, memory: str) -> None:
        try:
            data, addressed = self._parse_editor_details(memory)
            stats = analyze_memory(data, FLASH_PAGE_SIZE if memory == "flash" else EEPROM_PAGE_SIZE)
        except Exception as exc:
            messagebox.showerror("Program buffer", str(exc), parent=self)
            return
        original = self.memory_original_images.get(memory)
        changes = sum(1 for old, new in zip(original, data) if old != new) if original is not None else None
        changes_text = str(changes) if changes is not None else "unknown because the device has not been read in this session"
        backup = bool(self.operation_backup_var.get())
        verify = bool(self.operation_verify_var.get())
        full_readback = bool(self.operation_full_readback_var.get())
        restore_eeprom = bool(self.flash_restore_eeprom_var.get()) if memory == "flash" else False
        lines = [
            f"The complete {len(data)}-byte {memory} FILE / EDITOR buffer will be programmed.",
            f"Programmed bytes in buffer: {stats.programmed_bytes} ({stats.used_percent:.2f}%).",
            f"Bytes different from the last device read: {changes_text}.",
            "",
            f"Backup first: {'Yes' if backup else 'NO'}",
            f"Verify during write: {'Yes' if verify else 'NO'}",
            f"Second full readback: {'Yes' if full_readback else 'No'}",
        ]
        if memory == "flash":
            lines.append(f"Restore EEPROM after flash erase if required: {'Yes' if restore_eeprom else 'No'}")
        else:
            lines.append(f"Smart changed-page write: {'Yes' if self.eeprom_smart_write_var.get() else 'No'}")
        lines.extend([
            f"ISP speed: {self.isp_speed_var.get()}",
            "",
        ])
        if not backup:
            lines.append("WARNING: no complete backup package will be created.")
        if not verify and not full_readback:
            lines.append("WARNING: write verification is disabled.")
        lines.append("Fuse and lock bytes are not changed by this operation.")
        summary = "\n".join(lines)
        if not self._confirm("Program file/editor buffer", summary, phrase="YES", button_text="Program buffer"):
            return
        self._set_retry_memory_write(memory, False)
        self._save_operation_options()
        if memory == "flash":
            operation = lambda: self.service.write_flash_data(
                data,
                create_backup=backup,
                verify_after_write=verify,
                full_readback=full_readback,
                restore_eeprom=restore_eeprom,
            )
        else:
            if self.eeprom_smart_write_var.get():
                operation = lambda: self.service.write_eeprom_smart_data(
                    data,
                    create_backup=backup,
                    verify_after_write=verify,
                    full_readback=full_readback,
                )
            else:
                operation = lambda: self.service.write_eeprom_data(
                    data,
                    create_backup=backup,
                    verify_after_write=verify,
                    full_readback=full_readback,
                )
        self._run_task(
            f"Programming {memory} editor buffer",
            operation,
            lambda result: self._editor_upload_completed(memory, data, result),
        )

    def _editor_upload_completed(self, memory: str, data: bytes, result: object) -> None:
        self._memory_write_completed(memory, result, fallback_data=data, source="verified editor upload")

    def _memory_write_completed(
        self,
        memory: str,
        result: object,
        fallback_data: bytes = b"",
        source: str = "full post-write device readback",
    ) -> None:
        outcome = result  # type: ignore[assignment]
        if not isinstance(outcome, OperationOutcome):
            return
        if outcome.success:
            self._set_retry_memory_write(memory, False)
            actual = outcome.extra.get("readback", b"")
            actual_readback = bool(outcome.extra.get("actual_readback", False))
            target = outcome.extra.get("target_data", fallback_data)
            if actual_readback and isinstance(actual, bytes) and actual:
                self.memory_original_images[memory] = actual
                self.memory_images[memory] = actual
                self._display_memory_stats(memory, actual, source, True)
                if source == "verified editor upload":
                    self._load_memory_into_editor(memory, actual, source="Verified programmed buffer")
                self._select_memory_view(memory, "device")
            else:
                self._mark_device_readout_stale(memory)
                if isinstance(target, bytes) and target:
                    self.editor_images[memory] = bytearray(target)
                    self._update_editor_dirty_status(memory)
            self._append_log(f"{outcome.title}: {outcome.detail}")
            messagebox.showinfo(outcome.title, outcome.detail, parent=self)
        else:
            self._set_retry_memory_write(memory, bool(outcome.extra.get("retryable", False)))
            actual = outcome.extra.get("readback", b"")
            actual_readback = bool(outcome.extra.get("actual_readback", False))
            if actual_readback and isinstance(actual, bytes) and actual:
                self.memory_original_images[memory] = actual
                self.memory_images[memory] = actual
                self._display_memory_stats(memory, actual, "post-failure device readback", True)
                first = outcome.extra.get("first_mismatch")
                if isinstance(first, int):
                    editor = getattr(self, f"{memory}_editor_text")
                    editor.mark_set("insert", self._editor_index_for_byte(first, "ascii"))
                    editor.see(self._editor_index_for_byte(first, "ascii"))
                    self._highlight_editor_byte(memory)
                self._editor_status(
                    memory,
                    "WRITE FAILED — requested buffer is on the right; actual chip readback is on the left. Retry is available when safe.",
                )
                self._select_memory_view(memory, "device")
            self._show_outcome(outcome)

    def _write_flash(self) -> None:
        path = Path(self.flash_path_var.get())
        if not path.exists():
            messagebox.showerror("Flash write", "Select a valid firmware file first.", parent=self)
            return
        try:
            offset = parse_number(self.flash_offset_var.get())
            info = inspect_image(path, offset)
            preserve_start = parse_number(self.flash_preserve_start_var.get()) if self.flash_preserve_var.get() else None
        except Exception as exc:
            messagebox.showerror("Flash write", str(exc), parent=self)
            return
        merge_current = self.flash_merge_var.get()
        if path.suffix.lower() not in (".hex", ".ihx") and self.mode_var.get() == "Basic":
            merge_current = True
        backup = bool(self.operation_backup_var.get())
        verify = bool(self.operation_verify_var.get())
        full_readback = bool(self.operation_full_readback_var.get())
        period = self.service._effective_bitclock_us()
        summary = (
            f"SELECTED FILE\n"
            f"File: {path}\nType: {info.file_type}\nPayload: {info.occupied_bytes} bytes\n"
            f"Address range: 0x{info.minimum_address:04X}-0x{info.maximum_address:04X}\n\n"
            f"FLASH HANDLING\n"
            f"Merge unspecified addresses with current flash: {'Yes' if merge_current else 'No'}\n"
            f"Preserve upper region: {f'0x{preserve_start:04X}-0x1FFF' if preserve_start is not None else 'No'}\n"
            f"Restore EEPROM if chip erase clears it: {'Yes' if self.flash_restore_eeprom_var.get() else 'No'}\n\n"
            f"SELECTED SAFEGUARDS\n"
            f"Complete backup before writing: {'Yes' if backup else 'NO'}\n"
            f"AVRDUDE verification during write: {'Yes' if verify else 'NO'}\n"
            f"Read chip again after write (full 8192-byte check): {'Yes' if full_readback else 'No'}\n"
            f"ISP speed: {self.isp_speed_var.get()} (-B {period:g} µs)\n\n"
            + ("WARNING: No backup package will be created.\n" if not backup else "")
            + ("WARNING: The result will not be verified.\n" if not verify and not full_readback else "")
            + ("The DEVICE dashboard will be marked stale until you read the chip again.\n" if not full_readback else "The DEVICE dashboard will be refreshed from the post-write readback.\n")
        )
        phrase = "PROGRAM UNVERIFIED" if not verify and not full_readback else ""
        button_text = "Program without verification" if phrase else "Program selected file"
        if not self._confirm("Program flash", summary, phrase=phrase, button_text=button_text):
            return
        self._save_operation_options()
        self._run_task(
            "Programming flash",
            lambda: self.service.write_flash(
                path,
                offset=offset,
                merge_with_current=merge_current,
                preserve_start=preserve_start,
                restore_eeprom=self.flash_restore_eeprom_var.get(),
                create_backup=backup,
                verify_after_write=verify,
                full_readback=full_readback,
            ),
            lambda result: self._memory_write_completed("flash", result),
        )

    def _erase_flash(self) -> None:
        preserve = bool(self.flash_erase_preserve_eeprom_var.get())
        summary = (
            "This uses the AVR chip-erase command because flash cannot be returned to all 0xFF bytes with an ordinary flash write.\n\n"
            "The operation will:\n"
            "• Create a complete backup\n"
            "• Erase all 8192 flash bytes\n"
            "• Clear lock bits\n"
            "• Leave LFUSE, HFUSE, and EFUSE unchanged\n"
            "• Read flash back and display the result\n"
            f"• {'Preserve EEPROM using the backup and verify it' if preserve else 'Allow EEPROM to follow the current EESAVE fuse, then reread it'}\n\n"
            "Both DEVICE panes will be refreshed after the operation."
        )
        if not self._confirm(
            "Erase flash and reread", summary, phrase="YES", button_text="Erase flash and reread"
        ):
            return
        self._run_task(
            "Erasing flash",
            lambda: self.service.erase_flash(preserve_eeprom=preserve),
            self._flash_erase_completed,
        )

    def _flash_erase_completed(self, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if not isinstance(outcome, OperationOutcome):
            return
        flash = outcome.extra.get("readback", b"")
        eeprom = outcome.extra.get("eeprom_readback", b"")
        if isinstance(flash, bytes) and flash:
            self._display_memory_stats("flash", flash, "post-erase device readback", True)
        if isinstance(eeprom, bytes) and eeprom:
            self._display_memory_stats("eeprom", eeprom, "post-erase device readback", True)
        self._editor_status("flash", "Flash erase readback is shown on the left. The editor buffer was left unchanged.")
        self._editor_status("eeprom", "Post-erase EEPROM readback is shown on the left. The editor buffer was left unchanged.")
        self._select_memory_view("flash", "device")
        self._show_outcome(outcome)

    def _verify_flash(self) -> None:
        path = Path(self.flash_path_var.get())
        if not path.exists():
            messagebox.showerror("Compare chip to file", "Select a valid firmware file first.", parent=self)
            return
        try:
            offset = parse_number(self.flash_offset_var.get())
        except Exception as exc:
            messagebox.showerror("Compare chip to file", str(exc), parent=self)
            return
        self._run_task(
            "Comparing flash chip ↔ file",
            lambda: self.service.verify_image("flash", path, offset),
            lambda result: self._comparison_completed("flash", result),
        )

    # ---------- EEPROM ----------

    def _build_eeprom_tab(self) -> None:
        self.eeprom_path_var = tk.StringVar()
        self.eeprom_offset_var = tk.StringVar(value="0x0000")
        self.eeprom_merge_var = tk.BooleanVar(value=True)
        self.eeprom_smart_write_var = tk.BooleanVar(value=bool(self.settings.get("smart_eeprom_write", True)))
        self.eeprom_editor_format_var = tk.StringVar(value="Addressed hex")
        self.eeprom_editor_current_format = "Addressed hex"

        self._build_memory_workspace(self.eeprom_tab, "eeprom")

        device_controls = self.eeprom_device_controls_frame
        ttk.Label(device_controls, text="Device actions", style="Status.TLabel").grid(row=0, column=0, sticky="w")
        device_buttons = ttk.Frame(device_controls)
        device_buttons.grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Button(device_buttons, text="Read device", command=self._read_eeprom).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(device_buttons, text="Read + save…", command=self._read_eeprom_to_file).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(device_buttons, text="Blank check", command=lambda: self._blank_check("eeprom")).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(device_buttons, text="Erase + reread…", command=self._erase_eeprom).grid(row=0, column=3)

        file_controls = self.eeprom_file_controls_frame
        picker = ttk.Frame(file_controls)
        picker.grid(row=0, column=0, sticky="ew")
        picker.columnconfigure(1, weight=1)
        ttk.Label(picker, text="File:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        path_entry = ttk.Entry(picker, textvariable=self.eeprom_path_var, width=28)
        path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        path_entry.bind("<Return>", lambda _event: self._inspect_eeprom())
        ttk.Button(picker, text="Browse…", command=self._browse_eeprom).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(picker, text="Reload", command=self._inspect_eeprom).grid(row=0, column=3)

        buffer_tools = ttk.Frame(file_controls)
        buffer_tools.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(buffer_tools, text="Buffer tools", style="Status.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(buffer_tools, text="Copy device", command=lambda: self._load_device_into_editor("eeprom")).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(buffer_tools, text="New blank", command=lambda: self._new_blank_editor("eeprom")).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(buffer_tools, text="Undo", command=lambda: self._undo_editor("eeprom")).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(buffer_tools, text="Redo", command=lambda: self._redo_editor("eeprom")).grid(row=0, column=4, padx=(0, 4))
        ttk.Button(buffer_tools, text="Save…", command=lambda: self._save_editor("eeprom")).grid(row=0, column=5)

        self._build_operation_options(file_controls, "eeprom", 2)

        file_actions = ttk.Frame(file_controls)
        file_actions.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(file_actions, text="Editor actions", style="Status.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(file_actions, text="Program buffer", command=lambda: self._upload_editor("eeprom")).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(file_actions, text="Compare chip ↔ buffer", command=lambda: self._compare_editor_buffer("eeprom")).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(file_actions, text="Check buffer", command=lambda: self._validate_editor("eeprom")).grid(row=0, column=3)
        self.eeprom_retry_button = ttk.Button(
            file_actions,
            text="Retry same buffer",
            command=lambda: self._retry_memory_write("eeprom"),
        )
        self.eeprom_retry_button.grid(row=0, column=4, padx=(8, 0))
        self.eeprom_retry_button.grid_remove()

        self.eeprom_advanced_frame = ttk.LabelFrame(file_controls, text="Advanced buffer and file options", padding=4)
        self.eeprom_advanced_frame.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(self.eeprom_advanced_frame, text="View format:").grid(row=0, column=0, sticky="w")
        self.eeprom_editor_format_combo = ttk.Combobox(
            self.eeprom_advanced_frame,
            textvariable=self.eeprom_editor_format_var,
            state="readonly",
            width=14,
            values=("Addressed hex", "Intel HEX", "Raw hex bytes"),
        )
        self.eeprom_editor_format_combo.grid(row=0, column=1, padx=(4, 4), sticky="w")
        self.eeprom_editor_format_combo.bind("<<ComboboxSelected>>", lambda _event: self._convert_editor_format("eeprom"))
        ttk.Button(self.eeprom_advanced_frame, text="Convert", command=lambda: self._convert_editor_format("eeprom")).grid(row=0, column=2, padx=(0, 12))
        ttk.Label(self.eeprom_advanced_frame, text="BIN offset:").grid(row=0, column=3, sticky="w")
        offset_entry = ttk.Entry(self.eeprom_advanced_frame, textvariable=self.eeprom_offset_var, width=10)
        offset_entry.grid(row=0, column=4, padx=(4, 12), sticky="w")
        offset_entry.bind("<Return>", lambda _event: self._inspect_eeprom())
        ttk.Checkbutton(self.eeprom_advanced_frame, text="Merge unspecified bytes with current EEPROM", variable=self.eeprom_merge_var).grid(row=0, column=5, sticky="w")
        ttk.Checkbutton(
            self.eeprom_advanced_frame,
            text="Smart write: program changed 4-byte pages only",
            variable=self.eeprom_smart_write_var,
            command=lambda: self.settings.set("smart_eeprom_write", bool(self.eeprom_smart_write_var.get())),
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

    def _browse_eeprom(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Select EEPROM image", filetypes=[("EEPROM images", "*.hex *.ihx *.bin *.eep"), ("All files", "*.*")])
        if path:
            self.eeprom_path_var.set(path)
            self._inspect_eeprom()

    def _inspect_eeprom(self, silent: bool = False) -> bool:
        raw_path = self.eeprom_path_var.get().strip()
        if not raw_path:
            return False
        if self.editor_dirty.get("eeprom", False):
            if silent:
                return False
            if not messagebox.askyesno(
                "Replace EEPROM editor buffer",
                "Loading this file will replace unsaved edits in the EEPROM buffer. Continue?",
                parent=self,
            ):
                self.eeprom_path_var.set(self.editor_loaded_paths.get("eeprom", ""))
                return False
        try:
            selected_path = Path(raw_path)
            offset = parse_number(self.eeprom_offset_var.get())
            info = inspect_image(selected_path, offset=offset)
            image, _payload = load_image(selected_path, EEPROM_SIZE, offset=offset)
            self.eeprom_editor_format_var.set("Addressed hex")
            source = (
                f"File: {selected_path.name} — {info.file_type}; "
                f"addresses 0x{info.minimum_address:04X}-0x{info.maximum_address:04X}"
            )
            self._load_memory_into_editor("eeprom", image, source=source)
            self.editor_loaded_paths["eeprom"] = str(selected_path)
            self.status_var.set(f"Loaded {selected_path.name} into the EEPROM editor buffer")
            self._select_memory_view("eeprom", "file")
            return True
        except Exception as exc:
            self.eeprom_file_capacity_var.set("File could not be loaded")
            self.eeprom_file_details_var.set(f"{exc} Existing editor buffer was preserved.")
            self._editor_status("eeprom", f"File load failed: {exc}")
            if not silent:
                messagebox.showerror("EEPROM file load", str(exc), parent=self)
            return False

    def _read_eeprom(self) -> None:
        self._run_task("Reading EEPROM", lambda: self.service.read_memory("eeprom"), lambda result: self._memory_read_completed("eeprom", result))

    def _read_eeprom_to_file(self) -> None:
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Save EEPROM dump",
            defaultextension=".bin",
            filetypes=[("Raw binary", "*.bin"), ("Intel HEX", "*.hex"), ("All files", "*.*")],
        )
        if destination:
            self._run_task(
                "Reading and saving EEPROM",
                lambda: self.service.read_eeprom_to_file(Path(destination)),
                lambda result: self._memory_read_completed("eeprom", result),
            )

    def _write_eeprom(self) -> None:
        path = Path(self.eeprom_path_var.get())
        if not path.exists():
            messagebox.showerror("EEPROM write", "Select a valid EEPROM file first.", parent=self)
            return
        try:
            offset = parse_number(self.eeprom_offset_var.get())
            info = inspect_image(path, offset)
        except Exception as exc:
            messagebox.showerror("EEPROM write", str(exc), parent=self)
            return
        backup = bool(self.operation_backup_var.get())
        verify = bool(self.operation_verify_var.get())
        full_readback = bool(self.operation_full_readback_var.get())
        period = self.service._effective_bitclock_us()
        summary = (
            f"SELECTED FILE\n"
            f"File: {path}\nPayload: {info.occupied_bytes} bytes\n"
            f"Address range: 0x{info.minimum_address:04X}-0x{info.maximum_address:04X}\n\n"
            f"EEPROM HANDLING\n"
            f"Merge unspecified addresses with current EEPROM: {'Yes' if self.eeprom_merge_var.get() else 'No'}\n\n"
            f"SELECTED SAFEGUARDS\n"
            f"Complete backup before writing: {'Yes' if backup else 'NO'}\n"
            f"AVRDUDE verification during write: {'Yes' if verify else 'NO'}\n"
            f"Read chip again after write (full 512-byte check): {'Yes' if full_readback else 'No'}\n"
            f"ISP speed: {self.isp_speed_var.get()} (-B {period:g} µs)\n\n"
            + ("WARNING: No backup package will be created.\n" if not backup else "")
            + ("WARNING: The result will not be verified.\n" if not verify and not full_readback else "")
            + ("The DEVICE dashboard will be marked stale until you read the chip again.\n" if not full_readback else "The DEVICE dashboard will be refreshed from the post-write readback.\n")
        )
        phrase = "PROGRAM UNVERIFIED" if not verify and not full_readback else ""
        button_text = "Program without verification" if phrase else "Program selected file"
        if not self._confirm("Program EEPROM", summary, phrase=phrase, button_text=button_text):
            return
        self._save_operation_options()
        self._run_task(
            "Programming EEPROM",
            lambda: self.service.write_eeprom(
                path,
                offset,
                self.eeprom_merge_var.get(),
                create_backup=backup,
                verify_after_write=verify,
                full_readback=full_readback,
            ),
            lambda result: self._memory_write_completed("eeprom", result),
        )

    def _verify_eeprom(self) -> None:
        path = Path(self.eeprom_path_var.get())
        if not path.exists():
            messagebox.showerror("Compare chip to file", "Select a valid EEPROM file first.", parent=self)
            return
        try:
            offset = parse_number(self.eeprom_offset_var.get())
        except Exception as exc:
            messagebox.showerror("Compare chip to file", str(exc), parent=self)
            return
        self._run_task(
            "Comparing EEPROM chip ↔ file",
            lambda: self.service.verify_image("eeprom", path, offset),
            lambda result: self._comparison_completed("eeprom", result),
        )

    def _erase_eeprom(self) -> None:
        backup = bool(self.operation_backup_var.get())
        verify = bool(self.operation_verify_var.get())
        summary = (
            "All 512 EEPROM bytes will be written to 0xFF.\n\n"
            f"Complete backup before erasing: {'Yes' if backup else 'NO'}\n"
            f"AVRDUDE verification during write: {'Yes' if verify else 'NO'}\n"
            "Mandatory full reread after erase: Yes\n"
            "The EEPROM DEVICE pane will be refreshed so you can visually confirm every row is FF."
        )
        if self._confirm("Erase EEPROM and reread", summary, phrase="YES", button_text="Erase EEPROM and reread"):
            self._run_task(
                "Erasing EEPROM",
                lambda: self.service.erase_eeprom(
                    create_backup=backup,
                    verify_after_write=verify,
                    full_readback=True,
                ),
                lambda result: self._memory_write_completed("eeprom", result, source="post-erase device readback"),
            )

    def _blank_check(self, memory: str) -> None:
        self._run_task(f"Blank-checking {memory}", lambda: self.service.blank_check(memory))

    # ---------- fuses ----------

    def _build_fuses_tab(self) -> None:
        self._suppress_fuse_editor_events = True
        self.fuse_editor_dirty = False
        self.loaded_preset_name = ""
        self.preset_var = tk.StringVar(value=list(SAFE_PRESETS.keys())[0])
        saved_grade = normalize_device_grade(str(self.settings.get("attiny85_device_grade", DEVICE_GRADE_SELECT)))
        self.device_grade_var = tk.StringVar(value=saved_grade if saved_grade in DEVICE_GRADES else DEVICE_GRADE_SELECT)
        saved_supply = normalize_supply_label(str(self.settings.get("attiny85_supply_voltage", SUPPLY_SELECT)))
        self.supply_voltage_var = tk.StringVar(value=saved_supply if saved_supply in SUPPLY_VOLTAGES else SUPPLY_SELECT)
        self.voltage_verified_var = tk.BooleanVar(value=False)
        self.clock_hardware_ready_var = tk.BooleanVar(value=False)
        self.firmware_clock_ack_var = tk.BooleanVar(value=False)
        self.external_frequency_var = tk.StringVar(value="")
        self.clock_var = tk.StringVar(value="Internal RC 8 MHz")
        self.sut_var = tk.StringVar(value="10")
        self.divide_var = tk.BooleanVar(value=True)
        self.clock_output_var = tk.BooleanVar(value=False)
        self.watchdog_var = tk.BooleanVar(value=False)
        self.eesave_var = tk.BooleanVar(value=False)
        self.bod_var = tk.StringVar(value="Disabled")
        self.debugwire_var = tk.BooleanVar(value=False)
        self.reset_disabled_var = tk.BooleanVar(value=False)
        self.self_programming_var = tk.BooleanVar(value=False)
        self.raw_override_var = tk.BooleanVar(value=False)
        self.raw_lfuse_var = tk.StringVar(value="0x62")
        self.raw_hfuse_var = tk.StringVar(value="0xDF")
        self.raw_efuse_var = tk.StringVar(value="0xFF")

        content = ttk.Frame(self.fuses_tab)
        self.fuse_content = content
        content.grid(row=0, column=0, sticky="nsew")
        self.fuses_tab.columnconfigure(0, weight=1)
        self.fuses_tab.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        preset = ttk.LabelFrame(content, text="Fuse preset and programming", padding=6)
        preset.grid(row=0, column=0, sticky="ew")
        preset.columnconfigure(1, weight=1)
        ttk.Label(preset, text="Preset:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.preset_combo = ttk.Combobox(
            preset,
            textvariable=self.preset_var,
            state="readonly",
            values=tuple(SAFE_PRESETS.keys()),
            width=42,
        )
        self.preset_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.preset_combo.bind("<<ComboboxSelected>>", lambda _event: self._preset_selection_changed())
        ttk.Button(preset, text="Read current", command=self._detect).grid(row=0, column=2, padx=(0, 5))
        ttk.Button(preset, text="Load preset", command=self._apply_preset).grid(row=0, column=3, padx=(0, 5))
        ttk.Button(preset, text="Reset proposal", command=self._reset_fuse_editor).grid(row=0, column=4, padx=(0, 8))

        self.program_proposed_button_var = tk.StringVar(value="Read current fuses first")
        self.program_proposed_button = ttk.Button(
            preset,
            textvariable=self.program_proposed_button_var,
            command=self._write_fuses,
            state="disabled",
        )
        self.program_proposed_button.grid(row=0, column=5)
        self.program_preset_button_var = self.program_proposed_button_var
        self.program_preset_button = self.program_proposed_button

        self.fuse_editor_status_var = tk.StringVar(value="No preset is currently loaded into Proposed.")
        ttk.Label(
            preset,
            textvariable=self.fuse_editor_status_var,
            wraplength=1050,
            foreground="#7a4b00",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        body = ttk.Frame(content)
        body.grid(row=1, column=0, sticky="nsew", pady=(5, 0))
        body.columnconfigure(0, weight=5, uniform="fuse")
        body.columnconfigure(1, weight=6, uniform="fuse")
        body.rowconfigure(0, weight=1)

        settings_panel = ttk.Frame(body)
        settings_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        settings_panel.columnconfigure(0, weight=1)

        guided = ttk.LabelFrame(settings_panel, text="Operating setup", padding=6)
        self.fuse_guided_frame = guided
        guided.grid(row=0, column=0, sticky="ew")
        guided.columnconfigure(1, weight=1)
        ttk.Label(guided, text="Chip marking:").grid(row=0, column=0, sticky="w")
        self.device_grade_combo = ttk.Combobox(
            guided,
            textvariable=self.device_grade_var,
            state="readonly",
            values=tuple(DEVICE_GRADES.keys()),
            width=30,
        )
        self.device_grade_combo.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.device_grade_combo.bind("<<ComboboxSelected>>", lambda _event: self._device_grade_changed())
        ttk.Label(guided, text="Planned VCC:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.supply_voltage_combo = ttk.Combobox(
            guided,
            textvariable=self.supply_voltage_var,
            state="readonly",
            values=tuple(SUPPLY_VOLTAGES.keys()),
            width=22,
        )
        self.supply_voltage_combo.grid(row=1, column=1, sticky="w", padx=(5, 0), pady=(4, 0))
        self.supply_voltage_combo.bind("<<ComboboxSelected>>", lambda _event: self._planned_voltage_changed())

        self.external_frequency_frame = ttk.Frame(guided)
        self.external_frequency_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(self.external_frequency_frame, text="External frequency:").grid(row=0, column=0, sticky="w")
        self.external_frequency_entry = ttk.Entry(self.external_frequency_frame, textvariable=self.external_frequency_var, width=10)
        self.external_frequency_entry.grid(row=0, column=1, sticky="w", padx=(5, 3))
        ttk.Label(self.external_frequency_frame, text="MHz", foreground="#555555").grid(row=0, column=2, sticky="w")
        self.external_frequency_var.trace_add("write", lambda *_args: self._manual_external_frequency_changed())

        self.fuse_readiness_frame = ttk.LabelFrame(guided, text="Required confirmations", padding=4)
        self.fuse_readiness_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self.voltage_verified_check = ttk.Checkbutton(
            self.fuse_readiness_frame,
            text="VCC measured at pins 8 and 4 under normal load",
            variable=self.voltage_verified_var,
            command=self._clock_proof_changed,
        )
        self.voltage_verified_check.grid(row=0, column=0, sticky="w")
        self.clock_hardware_ready_check = ttk.Checkbutton(
            self.fuse_readiness_frame,
            text="External clock/crystal hardware is installed and running",
            variable=self.clock_hardware_ready_var,
            command=self._clock_proof_changed,
        )
        self.clock_hardware_ready_check.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.firmware_clock_ack_check = ttk.Checkbutton(
            self.fuse_readiness_frame,
            text="Firmware uses the shown F_CPU / external frequency",
            variable=self.firmware_clock_ack_var,
            command=self._clock_proof_changed,
        )
        self.firmware_clock_ack_check.grid(row=2, column=0, sticky="w", pady=(2, 0))

        self.clock_setup_status_var = tk.StringVar(value="Clock setup: not checked")
        self.clock_setup_status_label = ttk.Label(
            guided,
            textvariable=self.clock_setup_status_var,
            font=("Segoe UI", 10, "bold"),
        )
        self.clock_setup_status_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))

        self.fuse_advanced_frame = ttk.LabelFrame(settings_panel, text="Manual fuse settings", padding=6)
        self.fuse_advanced_frame.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.fuse_advanced_frame.columnconfigure(1, weight=1)
        ttk.Label(self.fuse_advanced_frame, text="Clock source:").grid(row=0, column=0, sticky="w")
        self.clock_combo = ttk.Combobox(self.fuse_advanced_frame, textvariable=self.clock_var, state="readonly", width=30)
        self.clock_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(5, 0))
        self.clock_combo.bind("<<ComboboxSelected>>", lambda _event: self._clock_changed(mark_dirty=True))
        ttk.Label(self.fuse_advanced_frame, text="Start-up:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.sut_combo = ttk.Combobox(self.fuse_advanced_frame, textvariable=self.sut_var, state="readonly", width=7)
        self.sut_combo.grid(row=1, column=1, sticky="w", padx=(5, 12), pady=(4, 0))
        self.sut_combo.bind("<<ComboboxSelected>>", lambda _event: self._fuse_editor_changed())
        ttk.Label(self.fuse_advanced_frame, text="BOD:").grid(row=1, column=2, sticky="w", pady=(4, 0))
        ttk.Combobox(self.fuse_advanced_frame, textvariable=self.bod_var, state="readonly", values=tuple(BOD_CODES.keys()), width=12).grid(row=1, column=3, sticky="w", padx=(5, 0), pady=(4, 0))
        ttk.Checkbutton(self.fuse_advanced_frame, text="Divide clock by 8", variable=self.divide_var, command=self._fuse_editor_changed).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(self.fuse_advanced_frame, text="Preserve EEPROM on chip erase", variable=self.eesave_var, command=self._fuse_editor_changed).grid(row=2, column=2, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Separator(self.fuse_advanced_frame).grid(row=3, column=0, columnspan=4, sticky="ew", pady=5)
        ttk.Checkbutton(self.fuse_advanced_frame, text="Clock output PB4", variable=self.clock_output_var, command=self._fuse_editor_changed).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(self.fuse_advanced_frame, text="Watchdog always on", variable=self.watchdog_var, command=self._fuse_editor_changed).grid(row=4, column=2, columnspan=2, sticky="w")
        ttk.Checkbutton(self.fuse_advanced_frame, text="Enable debugWIRE", variable=self.debugwire_var, command=self._fuse_editor_changed).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Checkbutton(self.fuse_advanced_frame, text="Disable RESET / use PB5", variable=self.reset_disabled_var, command=self._fuse_editor_changed).grid(row=5, column=2, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Checkbutton(self.fuse_advanced_frame, text="Enable self-programming", variable=self.self_programming_var, command=self._fuse_editor_changed).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Label(self.fuse_advanced_frame, text="SPIEN remains enabled for ISP.", foreground="#555555").grid(row=6, column=2, columnspan=2, sticky="w", pady=(2, 0))

        self.raw_fuse_frame = ttk.LabelFrame(settings_panel, text="Expert raw fuse bytes", padding=6)
        self.raw_fuse_frame.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        ttk.Checkbutton(
            self.raw_fuse_frame,
            text="Use raw bytes",
            variable=self.raw_override_var,
            command=self._fuse_editor_changed,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        for column, (label, variable) in enumerate((
            ("LFUSE", self.raw_lfuse_var),
            ("HFUSE", self.raw_hfuse_var),
            ("EFUSE", self.raw_efuse_var),
        ), start=1):
            ttk.Label(self.raw_fuse_frame, text=label).grid(row=0, column=column * 2 - 1, padx=(5, 2))
            ttk.Entry(self.raw_fuse_frame, textvariable=variable, width=8).grid(row=0, column=column * 2)
            variable.trace_add("write", lambda *_args: self._fuse_editor_changed())
        ttk.Label(
            self.raw_fuse_frame,
            text="Raw values can require external-clock or high-voltage recovery.",
            foreground="#9b1c1c",
        ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(4, 0))

        preview = ttk.LabelFrame(body, text="Current, proposed, and required changes", padding=6)
        preview.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(5, weight=1)
        self.current_fuse_label = ttk.Label(preview, text="Current: LFUSE --   HFUSE --   EFUSE --", wraplength=570)
        self.current_fuse_label.grid(row=0, column=0, sticky="w")
        self.proposed_fuse_label = ttk.Label(
            preview,
            text="Proposed (not written): LFUSE 0x62   HFUSE 0xDF   EFUSE 0xFF",
            font=("Segoe UI", 10, "bold"),
            wraplength=570,
        )
        self.proposed_fuse_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.fuse_activation_status_var = tk.StringVar(value="READ REQUIRED: read the chip fuses before programming")
        self.fuse_activation_status_label = ttk.Label(
            preview,
            textvariable=self.fuse_activation_status_var,
            font=("Segoe UI", 10, "bold"),
            foreground="#9b1c1c",
            wraplength=570,
        )
        self.fuse_activation_status_label.grid(row=2, column=0, sticky="w", pady=(3, 0))
        self.fuse_clock_label = ttk.Label(preview, text="Proposed result: Internal 1 MHz", wraplength=570)
        self.fuse_clock_label.grid(row=3, column=0, sticky="w", pady=(2, 0))

        review = ttk.Frame(preview)
        review.grid(row=5, column=0, sticky="nsew", pady=(5, 0))
        review.columnconfigure(0, weight=1)
        review.rowconfigure(0, weight=2)
        review.rowconfigure(1, weight=2)
        review.rowconfigure(2, weight=1)

        changes_box = ttk.LabelFrame(review, text="Required writes", padding=3)
        changes_box.grid(row=0, column=0, sticky="nsew")
        changes_box.rowconfigure(0, weight=1)
        changes_box.columnconfigure(0, weight=1)
        self.fuse_changes_text = tk.Text(changes_box, width=56, height=6, wrap="word")
        changes_scroll = ttk.Scrollbar(changes_box, orient="vertical", command=self.fuse_changes_text.yview)
        self.fuse_changes_text.configure(yscrollcommand=changes_scroll.set)
        self.fuse_changes_text.grid(row=0, column=0, sticky="nsew")
        changes_scroll.grid(row=0, column=1, sticky="ns")

        requirements_box = ttk.LabelFrame(review, text="Setup requirements", padding=3)
        requirements_box.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        requirements_box.rowconfigure(0, weight=1)
        requirements_box.columnconfigure(0, weight=1)
        self.clock_requirements_text = tk.Text(requirements_box, width=56, height=6, wrap="word")
        requirements_scroll = ttk.Scrollbar(requirements_box, orient="vertical", command=self.clock_requirements_text.yview)
        self.clock_requirements_text.configure(yscrollcommand=requirements_scroll.set)
        self.clock_requirements_text.grid(row=0, column=0, sticky="nsew")
        requirements_scroll.grid(row=0, column=1, sticky="ns")

        risks_box = ttk.LabelFrame(review, text="Warnings and recovery notes", padding=3)
        risks_box.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        risks_box.rowconfigure(0, weight=1)
        risks_box.columnconfigure(0, weight=1)
        self.fuse_risk_text = tk.Text(risks_box, width=56, height=4, wrap="word")
        risks_scroll = ttk.Scrollbar(risks_box, orient="vertical", command=self.fuse_risk_text.yview)
        self.fuse_risk_text.configure(yscrollcommand=risks_scroll.set)
        self.fuse_risk_text.grid(row=0, column=0, sticky="nsew")
        risks_scroll.grid(row=0, column=1, sticky="ns")

        for variable in (self.bod_var, self.clock_var, self.sut_var):
            variable.trace_add("write", lambda *_args: self._fuse_editor_changed())
        self._suppress_fuse_editor_events = False
        self._clock_changed(mark_dirty=False)

    def _reset_clock_readiness(self, reset_voltage_verification: bool = False) -> None:
        if not hasattr(self, "clock_hardware_ready_var"):
            return
        if reset_voltage_verification:
            self.voltage_verified_var.set(False)
        self.clock_hardware_ready_var.set(False)
        self.firmware_clock_ack_var.set(False)

    def _fuse_editor_changed(self) -> None:
        if getattr(self, "_suppress_fuse_editor_events", False):
            return
        self.loaded_preset_name = ""
        self.fuse_editor_dirty = True
        self._reset_clock_readiness(reset_voltage_verification=False)
        if hasattr(self, "fuse_editor_status_var"):
            self.fuse_editor_status_var.set(
                "PENDING: proposed fuse settings changed. The chip is unchanged. Review the exact required writes, "
                "complete any readiness checks, then use the fuse-programming button."
            )
        self._refresh_fuse_preview()

    def _preset_selection_changed(self) -> None:
        self.loaded_preset_name = ""
        self._reset_clock_readiness(reset_voltage_verification=False)
        self.fuse_editor_status_var.set(
            "A different preset is selected but not loaded. Click 'Load and review preset' to place its fuse values in Proposed."
        )
        self._refresh_fuse_preview()

    def _apply_preset(self) -> None:
        name = self.preset_var.get()
        config = SAFE_PRESETS[name]
        self._reset_clock_readiness(reset_voltage_verification=False)
        self.external_frequency_var.set("")
        self._load_config_into_editor(config, mark_dirty=True)
        self.loaded_preset_name = name
        self._refresh_fuse_preview()
        self.fuse_editor_status_var.set(
            f"PENDING: {name} is loaded into Proposed. It is not active until the required fuse bytes are programmed and verified."
        )

    def _program_selected_preset(self) -> None:
        name = self.preset_var.get()
        try:
            loaded_matches = self.loaded_preset_name == name and self._current_fuse_config().encode() == SAFE_PRESETS[name].encode()
        except Exception:
            loaded_matches = False
        if not loaded_matches:
            messagebox.showinfo(
                "Load and review the preset first",
                "The selected preset has not been loaded into Proposed. Click 'Load and review preset', inspect the required fuse writes, "
                "and complete any voltage, hardware, or firmware confirmations before programming.",
                parent=self,
            )
            return
        self._write_fuses()

    def _load_config_into_editor(self, config: FuseConfig, mark_dirty: bool = False) -> None:
        if not hasattr(self, "clock_var"):
            return
        previous_suppression = getattr(self, "_suppress_fuse_editor_events", False)
        self._suppress_fuse_editor_events = True
        try:
            self.clock_var.set(config.clock_source)
            self.sut_var.set(config.sut)
            self.divide_var.set(config.divide_by_8)
            self.clock_output_var.set(config.clock_output)
            self.watchdog_var.set(config.watchdog_always_on)
            self.eesave_var.set(config.preserve_eeprom)
            self.bod_var.set(config.bod if config.bod in BOD_CODES else "Disabled")
            self.debugwire_var.set(config.debugwire)
            self.reset_disabled_var.set(config.reset_disabled)
            self.self_programming_var.set(config.self_programming)
            self._clock_changed(mark_dirty=False)
        finally:
            self._suppress_fuse_editor_events = previous_suppression
        self.fuse_editor_dirty = mark_dirty
        if not mark_dirty:
            self.loaded_preset_name = ""
        self._refresh_fuse_preview()

    def _device_grade_changed(self) -> None:
        self.settings.set("attiny85_device_grade", self.device_grade_var.get())
        self._reset_clock_readiness(reset_voltage_verification=True)
        self._refresh_fuse_preview()

    def _planned_voltage_changed(self) -> None:
        self.settings.set("attiny85_supply_voltage", self.supply_voltage_var.get())
        self._reset_clock_readiness(reset_voltage_verification=True)
        self._refresh_fuse_preview()

    def _clock_proof_changed(self) -> None:
        self._refresh_fuse_preview()

    def _manual_external_frequency_changed(self) -> None:
        if getattr(self, "_suppress_fuse_editor_events", False):
            return
        self._reset_clock_readiness(reset_voltage_verification=True)
        self._refresh_fuse_preview()

    def _external_frequency_mhz(self) -> Optional[float]:
        text = self.external_frequency_var.get().strip()
        if not text:
            return None
        value = float(text)
        if value <= 0:
            raise ValueError("External frequency must be greater than 0 MHz.")
        if value > 100:
            raise ValueError("External frequency is not plausible for an ATtiny85. Enter MHz, for example 20 for 20 MHz.")
        return value

    # Compatibility wrapper for older internal calls.
    def _clock_environment_changed(self) -> None:
        self.settings.set("attiny85_device_grade", self.device_grade_var.get())
        self.settings.set("attiny85_supply_voltage", self.supply_voltage_var.get())
        self._refresh_fuse_preview()

    def _matching_preset_name(self, config: FuseConfig) -> str:
        name = getattr(self, "loaded_preset_name", "")
        if not name or name not in SAFE_PRESETS:
            return ""
        try:
            return name if SAFE_PRESETS[name].encode() == config.encode() else ""
        except Exception:
            return ""

    def _reset_fuse_editor(self) -> None:
        state = self.current_state
        if state.lfuse is None or state.hfuse is None or state.efuse is None:
            messagebox.showinfo("Fuses", "Detect the chip first.", parent=self)
            return
        self.raw_override_var.set(False)
        self._reset_clock_readiness(reset_voltage_verification=False)
        self._load_config_into_editor(FuseConfig.decode(state.lfuse, state.hfuse, state.efuse), mark_dirty=False)
        self.fuse_editor_status_var.set("Editor reset to the fuse values most recently read from the chip.")

    def _clock_changed(self, mark_dirty: bool = False) -> None:
        source = CLOCK_SOURCES.get(self.clock_var.get(), CLOCK_SOURCES["Internal RC 8 MHz"])
        values = tuple(source["sut"])
        self.sut_combo.configure(values=values)
        if self.sut_var.get() not in values:
            previous_suppression = getattr(self, "_suppress_fuse_editor_events", False)
            self._suppress_fuse_editor_events = True
            try:
                self.sut_var.set(str(source["default_sut"]))
            finally:
                self._suppress_fuse_editor_events = previous_suppression
        if mark_dirty:
            self._fuse_editor_changed()
        else:
            self._refresh_fuse_preview()

    def _current_fuse_config(self) -> FuseConfig:
        return FuseConfig(
            clock_source=self.clock_var.get(),
            sut=self.sut_var.get(),
            divide_by_8=self.divide_var.get(),
            clock_output=self.clock_output_var.get(),
            watchdog_always_on=self.watchdog_var.get(),
            preserve_eeprom=self.eesave_var.get(),
            bod=self.bod_var.get(),
            debugwire=self.debugwire_var.get(),
            reset_disabled=self.reset_disabled_var.get(),
            self_programming=self.self_programming_var.get(),
        )

    def _raw_fuse_values(self) -> Tuple[int, int, int]:
        return (
            parse_number(self.raw_lfuse_var.get()),
            parse_number(self.raw_hfuse_var.get()),
            parse_number(self.raw_efuse_var.get()),
        )

    def _refresh_fuse_preview(self) -> None:
        if not hasattr(self, "proposed_fuse_label"):
            return
        try:
            config = self._current_fuse_config()
            raw = self.raw_override_var.get() and self.mode_var.get() == "Expert"
            values = self._raw_fuse_values() if raw else config.encode()
            lfuse, hfuse, efuse = values
            effective_config = FuseConfig.decode(lfuse, hfuse, efuse) if raw else config
            preset_name = "" if raw else self._matching_preset_name(config)
            state = self.current_state
            current_values: Optional[Tuple[int, int, int]] = None
            if state.is_attiny85 and state.lfuse is not None and state.hfuse is not None and state.efuse is not None:
                current_values = (state.lfuse, state.hfuse, state.efuse)
            plan = fuse_change_plan(current_values, values, preset_name)
            report = clock_setup_report(
                effective_config,
                self.device_grade_var.get(),
                self.supply_voltage_var.get(),
                preset_name,
                voltage_verified=self.voltage_verified_var.get(),
                clock_hardware_ready=self.clock_hardware_ready_var.get(),
                firmware_clock_acknowledged=self.firmware_clock_ack_var.get(),
                external_frequency_mhz=self._external_frequency_mhz(),
            )
            self.proposed_fuse_label.configure(
                text=f"Proposed (not written): LFUSE 0x{lfuse:02X}   HFUSE 0x{hfuse:02X}   EFUSE 0x{efuse:02X}"
            )
            self.fuse_clock_label.configure(
                text="Proposed result after verified fuse write: " + effective_config.system_clock_description(preset_name)
            )

            if not plan.current_known:
                activation_text = "READ REQUIRED: current fuses are unknown; programming is disabled"
                activation_color = "#9b1c1c"
            elif plan.active:
                activation_text = "ACTIVE / VERIFIED: current fuse bytes already match Proposed"
                activation_color = "#176b2c"
            else:
                names = ", ".join(plan.changed_fuses)
                activation_text = f"PENDING: {names} must be written before this clock/configuration becomes active"
                activation_color = "#9b4f00"
            self.fuse_activation_status_var.set(activation_text)
            self.fuse_activation_status_label.configure(foreground=activation_color)

            change_lines = plan.formatted_lines()
            change_lines.extend([
                "",
                "Programming note: this action writes hardware fuse bytes. It does not upload firmware or reset fuses to factory defaults.",
                "A chip erase clears flash and lock bits but leaves these fuse settings unchanged.",
            ])
            self._set_text(self.fuse_changes_text, "\n".join(change_lines))

            requirement_lines: List[str] = []
            if preset_name:
                requirement_lines.append("Loaded guided preset: " + preset_name)
            elif self.loaded_preset_name:
                requirement_lines.append("The editor no longer exactly matches the loaded preset; it is now a custom proposal.")
            requirement_lines.extend(report.requirements)
            if report.errors:
                requirement_lines.append("\nNOT READY / BLOCKED:")
                requirement_lines.extend("- " + item for item in report.errors)
            if report.warnings:
                requirement_lines.append("\nWARNINGS:")
                requirement_lines.extend("- " + item for item in report.warnings)
            self._set_text(self.clock_requirements_text, "\n".join(requirement_lines))
            self.clock_setup_status_var.set("Clock setup: " + report.status)
            status_color = "#9b1c1c" if report.errors else ("#8a5a00" if report.warnings else "#176b2c")
            self.clock_setup_status_label.configure(foreground=status_color)

            source = CLOCK_SOURCES[effective_config.clock_source]
            external = source.get("risk") == "High"
            frequency = report.frequency_mhz
            high_speed = frequency is not None and frequency > 10.0
            manual_external = external and effective_config.nominal_frequency_mhz(preset_name) is None
            self.external_frequency_entry.configure(state="normal")
            if manual_external:
                self.external_frequency_frame.grid()
            else:
                self.external_frequency_frame.grid_remove()

            show_voltage = high_speed
            show_hardware = external
            show_fcpu = high_speed or external
            if show_voltage:
                self.voltage_verified_check.grid()
            else:
                self.voltage_verified_check.grid_remove()
            if show_hardware:
                self.clock_hardware_ready_check.grid()
            else:
                self.clock_hardware_ready_check.grid_remove()
            if show_fcpu:
                self.firmware_clock_ack_check.grid()
            else:
                self.firmware_clock_ack_check.grid_remove()
            if show_voltage or show_hardware or show_fcpu:
                self.fuse_readiness_frame.grid()
            else:
                self.fuse_readiness_frame.grid_remove()

            lines: List[str] = []
            errors = validate_raw_fuses(lfuse, hfuse, efuse) if raw else []
            for error in errors:
                lines.append("BLOCKED: " + error)
            for error in report.errors:
                lines.append("BLOCKED: " + error)
            for risk in effective_config.risk_items(raw_override=raw):
                lines.append(f"{risk.level}: {risk.title} - {risk.detail}")
            for warning in report.warnings:
                lines.append("WARNING: " + warning)
            if not lines:
                lines.append("Risk: Low. Guided settings and the selected operating environment are consistent.")
            lines.append("\nStart-up selection: " + STARTUP_DESCRIPTIONS.get(effective_config.sut, "Source-specific."))
            self._set_text(self.fuse_risk_text, "\n".join(lines))

            changed_text = "/".join(plan.changed_fuses)
            can_program = plan.current_known and plan.requires_programming and not report.errors and not errors
            loaded_matches = bool(preset_name and self.loaded_preset_name == self.preset_var.get())
            if not plan.current_known:
                program_button_text = "Read current fuses first"
            elif not plan.requires_programming:
                program_button_text = "Proposed settings already active"
            elif report.errors or errors:
                program_button_text = "Resolve highlighted checks first"
            else:
                program_button_text = f"Program {changed_text} and verify..."
            self.program_proposed_button_var.set(program_button_text)
            self.program_proposed_button.configure(state="normal" if can_program else "disabled")
        except Exception as exc:
            self.proposed_fuse_label.configure(text="Proposed: invalid")
            self.clock_setup_status_var.set("Clock setup: invalid")
            self.clock_setup_status_label.configure(foreground="#9b1c1c")
            self._set_text(self.fuse_risk_text, str(exc))
            if hasattr(self, "fuse_changes_text"):
                self._set_text(self.fuse_changes_text, str(exc))
            if hasattr(self, "clock_requirements_text"):
                self._set_text(self.clock_requirements_text, str(exc))
            if hasattr(self, "program_proposed_button"):
                self.program_proposed_button.configure(state="disabled")

    def _write_fuses(self) -> None:
        state = self.current_state
        if not state.is_attiny85 or state.lfuse is None or state.hfuse is None or state.efuse is None:
            messagebox.showerror(
                "Read chip fuses first",
                "ATtiny85 Explorer must identify the ATtiny85 and read its current LFUSE, HFUSE, and EFUSE before it can show or program the required changes.",
                parent=self,
            )
            return
        if state.lock is not None and (state.lock & 0x03) != 0x03:
            messagebox.showerror(
                "Chip is locked",
                "The current lock bits restrict fuse programming. Use the Lock Bits tab to create a backup and perform a chip erase to unlock it. "
                "Chip erase does not reset the fuses.",
                parent=self,
            )
            return
        try:
            config = self._current_fuse_config()
            raw = self.raw_override_var.get() and self.mode_var.get() == "Expert"
            raw_values = self._raw_fuse_values() if raw else None
            if raw_values:
                errors = validate_raw_fuses(*raw_values)
                if errors:
                    messagebox.showerror("Raw fuse validation", "\n".join(errors), parent=self)
                    return
                values = raw_values
                effective_config = FuseConfig.decode(*raw_values)
                preset_name = ""
            else:
                values = config.encode()
                effective_config = config
                preset_name = self._matching_preset_name(config)
            current_values = (state.lfuse, state.hfuse, state.efuse)
            plan = fuse_change_plan(current_values, values, preset_name)
            if not plan.requires_programming:
                messagebox.showinfo(
                    "Fuses already match",
                    "The current fuse bytes already match Proposed. No fuse programming is required.",
                    parent=self,
                )
                return
            risks = effective_config.risk_items(raw_override=raw)
            report = clock_setup_report(
                effective_config,
                self.device_grade_var.get(),
                self.supply_voltage_var.get(),
                preset_name,
                voltage_verified=self.voltage_verified_var.get(),
                clock_hardware_ready=self.clock_hardware_ready_var.get(),
                firmware_clock_acknowledged=self.firmware_clock_ack_var.get(),
                external_frequency_mhz=self._external_frequency_mhz(),
            )
        except Exception as exc:
            messagebox.showerror("Fuse editor", str(exc), parent=self)
            return

        if report.errors:
            messagebox.showerror(
                "Clock setup blocked",
                "ATtiny85 Explorer will not program this clock configuration until these checks are resolved:\n\n"
                + "\n".join("- " + item for item in report.errors),
                parent=self,
            )
            return

        phrase = report.confirmation_phrase
        for risk in risks:
            if risk.phrase and not phrase:
                phrase = risk.phrase
            if risk.phrase and risk.level == "Critical":
                phrase = risk.phrase
                break
        risk_text = "\n".join(f"{risk.level}: {risk.title}\n{risk.detail}" for risk in risks) or "Risk: Low"
        warning_text = "\n".join("WARNING: " + item for item in report.warnings)
        requirements_text = "\n".join(report.requirements)
        changes_text = "\n".join(plan.formatted_lines())
        summary = (
            "THIS ACTION WILL WRITE ATtiny85 HARDWARE FUSE BYTES.\n"
            "Selecting the clock in the editor did not change the chip; approving this dialog will.\n\n"
            f"Current: LFUSE 0x{state.lfuse:02X}   HFUSE 0x{state.hfuse:02X}   EFUSE 0x{state.efuse:02X}\n"
            f"Target:  LFUSE 0x{values[0]:02X}   HFUSE 0x{values[1]:02X}   EFUSE 0x{values[2]:02X}\n\n"
            f"REQUIRED WRITES AND SETTING CHANGES\n{changes_text}\n\n"
            f"CLOCK SETUP: {report.status}\n{requirements_text}\n\n"
            f"{risk_text}"
            + ("\n\n" + warning_text if warning_text else "")
            + "\n\nA complete flash/EEPROM/fuse/lock backup is created first. Only changed fuse bytes are written, with the clock fuse written last when applicable. "
              "The chip is then read back and the exact bytes must match before ATtiny85 Explorer reports success. Chip erase does not restore factory fuses."
        )
        if not self._confirm("Program required ATtiny85 fuses", summary, phrase=phrase, button_text="Write required fuses"):
            return
        self._run_task("Writing and verifying required fuses", lambda: self.service.write_fuses(config, raw_values=raw_values), self._fuses_written)

    def _fuses_written(self, result: object) -> None:
        outcome = result  # type: ignore[assignment]
        if outcome.success:
            self.fuse_editor_dirty = False
            self.fuse_editor_status_var.set(
                "Fuse programming completed and readback matched. Refreshing the current fuse display..."
            )
        else:
            self.fuse_editor_status_var.set(
                "Fuse programming was not verified. The proposed values remain in the editor; review the outcome and Logs before retrying."
            )
        self._show_outcome(outcome)
        self._detect()

    # ---------- lock bits ----------

    def _build_lock_tab(self) -> None:
        self.current_lock_var = tk.StringVar(value="--")
        self.lock_description_var = tk.StringVar(value="Detect the chip to read lock bits.")
        self.lock_choice_var = tk.StringVar(value="unlocked")

        self.lock_mode_notice_frame = ttk.LabelFrame(self.lock_tab, text="Expert mode required for lock changes", padding=10)
        self.lock_mode_notice_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.lock_mode_notice_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.lock_mode_notice_frame,
            text=(
                "Nothing is broken: Basic and Advanced modes can read lock bits, but programming lock protection or performing the destructive unlock erase is available only in Expert mode."
            ),
            wraplength=780,
            foreground="#7a4a00",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(self.lock_mode_notice_frame, text="Switch to Expert mode", command=self._switch_to_expert).grid(row=0, column=1, padx=(12, 0))

        current = ttk.LabelFrame(self.lock_tab, text="Current lock state", padding=12)
        current.grid(row=1, column=0, sticky="ew")
        ttk.Label(current, textvariable=self.current_lock_var, font=("Consolas", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(current, textvariable=self.lock_description_var, wraplength=760).grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Button(current, text="Read lock bits", command=self._detect).grid(row=0, column=1, rowspan=2, padx=(16, 0))
        current.columnconfigure(0, weight=1)

        choices = ttk.LabelFrame(self.lock_tab, text="Protection mode", padding=12)
        choices.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.lock_mode1_radio = ttk.Radiobutton(
            choices,
            text="Mode 1 - Unlocked (0xFF; reached by chip erase, not programmed)",
            variable=self.lock_choice_var,
            value="unlocked",
        )
        self.lock_mode1_radio.pack(anchor="w")
        self.lock_mode2_radio = ttk.Radiobutton(
            choices,
            text="Mode 2 - Disable further flash/EEPROM programming and lock fuses (0xFE)",
            variable=self.lock_choice_var,
            value="mode2",
        )
        self.lock_mode2_radio.pack(anchor="w", pady=(7, 0))
        self.lock_mode3_radio = ttk.Radiobutton(
            choices,
            text="Mode 3 - Disable further programming and verification; lock fuses (0xFC)",
            variable=self.lock_choice_var,
            value="mode3",
        )
        self.lock_mode3_radio.pack(anchor="w", pady=(7, 0))

        info = ttk.LabelFrame(self.lock_tab, text="Important", padding=12)
        info.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        ttk.Label(
            info,
            text=(
                "Lock bits only change from 1 to 0 through programming. They return to the unlocked state only after a chip erase. "
                "A chip erase clears flash and lock bits; EEPROM is erased unless it is preserved. Clock, BOD, and all other fuse settings remain unchanged. "
                "Configure and verify fuses before programming Mode 2 or Mode 3 lock protection."
            ),
            wraplength=850,
        ).pack(anchor="w")

        actions = ttk.Frame(self.lock_tab)
        actions.grid(row=4, column=0, sticky="e", pady=(10, 0))
        self.lock_erase_button = ttk.Button(actions, text="Erase memory and unlock (fuses unchanged)", command=self._chip_erase)
        self.lock_erase_button.grid(row=0, column=0, padx=(0, 8))
        self.lock_program_button = ttk.Button(actions, text="Program selected lock mode", command=self._write_lock)
        self.lock_program_button.grid(row=0, column=1)
        self.lock_tab.columnconfigure(0, weight=1)
        self.lock_tab.rowconfigure(3, weight=1)

    def _switch_to_expert(self) -> None:
        self.mode_var.set("Expert")
        self._mode_changed()
        self.status_var.set("Expert mode enabled for lock-bit controls")

    def _write_lock(self) -> None:
        if self.mode_var.get() != "Expert":
            messagebox.showinfo("Expert mode required", "Switch to Expert mode to program lock bits.", parent=self)
            return
        choice = self.lock_choice_var.get()
        if choice == "unlocked":
            if self.current_state.lock is not None and (self.current_state.lock & 0x03) == 0x03:
                messagebox.showinfo("Lock bits", "The chip is already in Mode 1 (unlocked). No lock-bit write is required.", parent=self)
            else:
                messagebox.showinfo(
                    "Lock bits",
                    "Mode 1 is restored by chip erase, not by programming 0xFF. Use 'Erase flash/EEPROM and unlock (fuses unchanged)'.",
                    parent=self,
                )
            return
        value = 0xFE if choice == "mode2" else 0xFC
        detail = decode_lock(value)
        if not self._confirm(
            "Program lock bits",
            f"{detail}\n\nThis prevents ordinary programming. Lock bits can be removed only with a chip erase. A complete backup is created first.",
            phrase="LOCK THIS CHIP",
            button_text="Program lock bits",
        ):
            return
        self._run_task("Programming lock bits", lambda: self.service.write_lock(value), self._lock_written)

    def _lock_written(self, result: object) -> None:
        self._show_outcome(result)  # type: ignore[arg-type]
        self._detect()

    def _chip_erase(self) -> None:
        if self.mode_var.get() != "Expert":
            messagebox.showinfo("Expert mode required", "Switch to Expert mode to erase memory and clear lock bits.", parent=self)
            return
        if not self._confirm(
            "Chip erase",
            "This erases flash and clears lock bits. EEPROM is erased unless the current EESAVE fuse preserves it. "
            "Clock, BOD, EEPROM-preserve, RESET, and all other fuse settings remain unchanged. A complete backup is created first.",
            phrase="ERASE ATTINY85",
            button_text="Erase memory and unlock",
        ):
            return
        self._run_task("Erasing chip", self.service.chip_erase, self._chip_erased)

    def _chip_erased(self, result: object) -> None:
        self._show_outcome(result)  # type: ignore[arg-type]
        self._detect()

    # ---------- backends and manual ----------

    def _build_backends_tab(self) -> None:
        tree_frame = ttk.LabelFrame(self.backends_tab, text="AVRDUDE backends", padding=8)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        columns = ("name", "version", "preferred", "fallback", "status")
        self.backend_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=10)
        for column, title, width in [
            ("name", "Name", 300), ("version", "Version", 100), ("preferred", "Preferred", 75),
            ("fallback", "Fallback", 75), ("status", "Last test", 180)
        ]:
            self.backend_tree.heading(column, text=title)
            self.backend_tree.column(column, width=width, anchor="w")
        self.backend_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.backend_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.backend_tree.configure(yscrollcommand=scroll.set)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        buttons = ttk.Frame(self.backends_tab)
        buttons.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for column, (text, command) in enumerate([
            ("Backend folder...", self._select_arduino_directory),
            ("Auto-discover", self._auto_discover_backends),
            ("Add backend", self._add_backend),
            ("Remove", self._remove_backend),
            ("Test read-only", self._test_backend),
            ("Set preferred", self._set_preferred_backend),
            ("Set fallback", self._set_fallback_backend),
        ]):
            ttk.Button(buttons, text=text, command=command).grid(row=0, column=column, padx=(0, 7))

        detail = ttk.LabelFrame(self.backends_tab, text="Backend behavior", padding=10)
        detail.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(
            detail,
            text=(
                "The preferred backend handles normal operations. The fallback backend is used for read-only recovery and verification when a newer backend fails. "
                "A failed write is never blindly repeated. ATtiny85 Explorer reads back the chip and determines what actually happened. "
                "The verified AVRDUDE 6.3 backend is built into the application and is available without Arduino being installed. "
                "Advanced users can still register another complete AVRDUDE folder containing the executable, matching configuration, and companion DLLs."
            ),
            wraplength=900,
        ).pack(anchor="w")

        self.manual_frame = ttk.LabelFrame(self.backends_tab, text="Expert manual AVRDUDE arguments", padding=10)
        self.manual_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.manual_args_var = tk.StringVar(value="-v -U lfuse:r:-:h")
        ttk.Entry(self.manual_frame, textvariable=self.manual_args_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(self.manual_frame, text="Run", command=self._run_manual).grid(row=0, column=1)
        ttk.Label(
            self.manual_frame,
            text="ATtiny85 Explorer automatically prepends the selected executable, matching -C configuration, -p t85, and -c usbtiny.",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.manual_frame.columnconfigure(0, weight=1)

        self.backends_tab.columnconfigure(0, weight=1)
        self.backends_tab.rowconfigure(0, weight=1)

    def _refresh_backend_tree(self) -> None:
        if not hasattr(self, "backend_tree"):
            return
        for item in self.backend_tree.get_children():
            self.backend_tree.delete(item)
        for backend in self.registry.backends:
            status = backend.last_test_status
            if not backend.exe.is_file() and not backend.conf.is_file():
                status = "Missing executable and config"
            elif not backend.exe.is_file():
                status = "Missing avrdude.exe"
            elif not backend.conf.is_file():
                status = "Missing avrdude.conf"
            self.backend_tree.insert(
                "", "end", iid=backend.backend_id,
                values=(backend.name, backend.detected_version or "--", "Yes" if backend.preferred else "", "Yes" if backend.fallback else "", status),
            )
        preferred = self.registry.preferred()
        self.backend_var.set(preferred.name if preferred else "No backend configured")

    def _selected_backend(self) -> Optional[Backend]:
        selection = self.backend_tree.selection()
        if not selection:
            messagebox.showinfo("Backends", "Select a backend first.", parent=self)
            return None
        return self.registry.get(selection[0])

    def _auto_discover_backends(self) -> None:
        added = self.registry.auto_discover()
        self._refresh_backend_tree()
        messagebox.showinfo("Backend discovery", f"Found {len(added)} new backend(s).", parent=self)

    def _select_arduino_directory(self) -> None:
        initial = str(self.settings.get("arduino_directory", "")).strip()
        if not initial or not Path(initial).is_dir():
            initial = str(Path.home())
        selected = filedialog.askdirectory(
            parent=self,
            title="Select an AVRDUDE, Arduino IDE, or Arduino15 directory",
            initialdir=initial,
            mustexist=True,
        )
        if not selected:
            return

        selected_path = Path(selected)
        matches = self.registry.find_in_directory(selected_path)
        if not matches:
            messagebox.showerror(
                "Backend folder",
                "No complete AVRDUDE backend was found below that folder.\n\n"
                "ATtiny85 Explorer needs both avrdude.exe and its matching avrdude.conf. "
                "You may select the Arduino IDE root, hardware\\tools\\avr, an Arduino15 packages folder, or the backend's bin folder.",
                parent=self,
            )
            return

        added, updated = self.registry.add_from_directory(selected_path)
        self.settings.set("arduino_directory", str(selected_path))
        self._refresh_backend_tree()

        matched_paths = {(str(exe).lower(), str(conf).lower()) for exe, conf in matches}
        existing = [
            backend for backend in self.registry.backends
            if (str(backend.exe).lower(), str(backend.conf).lower()) in matched_paths
            and backend not in added
            and backend not in updated
        ]
        affected = updated + added + existing

        selected_backend = affected[0]
        if self.backend_tree.exists(selected_backend.backend_id):
            self.backend_tree.selection_set(selected_backend.backend_id)
            self.backend_tree.focus(selected_backend.backend_id)
            self.backend_tree.see(selected_backend.backend_id)

        messagebox.showinfo(
            "Backend folder",
            f"Backend paths updated: {len(updated)}\n"
            f"New backends added: {len(added)}\n"
            f"Already registered: {len(existing)}\n\n"
            "The selected location has been saved. Connect the programmer and run Test read-only before writing.",
            parent=self,
        )

    def _add_backend(self) -> None:
        exe = filedialog.askopenfilename(parent=self, title="Select avrdude.exe", filetypes=[("AVRDUDE", "avrdude.exe"), ("Executables", "*.exe"), ("All files", "*.*")])
        if not exe:
            return
        conf = filedialog.askopenfilename(parent=self, title="Select matching avrdude.conf", filetypes=[("AVRDUDE config", "avrdude.conf"), ("Configuration", "*.conf"), ("All files", "*.*")])
        if not conf:
            return
        name = simpledialog.askstring("Backend name", "Name for this backend:", initialvalue=f"AVRDUDE at {Path(exe).parent}", parent=self)
        if not name:
            return
        self.registry.add(name, Path(exe), Path(conf), known_good=False)
        self._refresh_backend_tree()

    def _remove_backend(self) -> None:
        backend = self._selected_backend()
        if backend and backend.built_in:
            messagebox.showinfo(
                "Built-in backend",
                "The verified built-in AVRDUDE 6.3 backend is part of ATtiny85 Explorer and cannot be removed.",
                parent=self,
            )
            return
        if backend and messagebox.askyesno("Remove backend", f"Remove {backend.name} from ATtiny85 Explorer?\n\nNo files are deleted.", parent=self):
            self.registry.remove(backend.backend_id)
            self._refresh_backend_tree()

    def _test_backend(self) -> None:
        backend = self._selected_backend()
        if backend:
            self._run_task(f"Testing {backend.name}", lambda: self.service.test_backend(backend), self._backend_tested)

    def _backend_tested(self, result: object) -> None:
        state, run_result = result  # type: ignore[misc]
        self._refresh_backend_tree()
        if run_result.ok and state.is_attiny85:
            messagebox.showinfo("Backend test", f"Compatible.\nVersion: {run_result.version or '--'}\nSignature: {state.signature}", parent=self)
        else:
            messagebox.showerror("Backend test", f"Failed: {run_result.classification}\n\nSee Logs for details.", parent=self)

    def _set_preferred_backend(self) -> None:
        backend = self._selected_backend()
        if backend:
            self.registry.set_preferred(backend.backend_id)
            self._refresh_backend_tree()

    def _set_fallback_backend(self) -> None:
        backend = self._selected_backend()
        if backend:
            try:
                self.registry.set_fallback(backend.backend_id)
            except ValueError as exc:
                messagebox.showinfo("Recovery backend", str(exc), parent=self)
            self._refresh_backend_tree()

    def _run_manual(self) -> None:
        if self.mode_var.get() != "Expert":
            return
        args = self.manual_args_var.get()
        if not self._confirm(
            "Run manual AVRDUDE arguments",
            f"Arguments:\n{args}\n\nManual mode can bypass normal operation planning. The command and output are logged.",
            phrase="RUN MANUAL COMMAND",
            button_text="Run command",
        ):
            return
        self._run_task("Running manual command", lambda: self.service.manual(args))


    # ---------- device serial terminal ----------

    def _build_terminal_tab(self) -> None:
        self.terminal_port_var = tk.StringVar(value=str(self.settings.get("terminal_port", "")))
        self.terminal_baud_var = tk.StringVar(value=str(self.settings.get("terminal_baud", "9600")))
        self.terminal_data_bits_var = tk.StringVar(value=str(self.settings.get("terminal_data_bits", "8")))
        self.terminal_parity_var = tk.StringVar(value=str(self.settings.get("terminal_parity", "None")))
        self.terminal_stop_bits_var = tk.StringVar(value=str(self.settings.get("terminal_stop_bits", "1")))
        self.terminal_flow_var = tk.StringVar(value=str(self.settings.get("terminal_flow_control", "None")))
        self.terminal_dtr_var = tk.BooleanVar(value=False)
        self.terminal_rts_var = tk.BooleanVar(value=False)
        self.terminal_status_var = tk.StringVar(value="Disconnected")
        self.terminal_modem_var = tk.StringVar(value="CTS --   DSR --   RI --   CD --")
        self.terminal_count_var = tk.StringVar(value="RX 0 bytes   TX 0 bytes")
        self.terminal_display_var = tk.StringVar(value=str(self.settings.get("terminal_display", "Text")))
        self.terminal_encoding_var = tk.StringVar(value=str(self.settings.get("terminal_encoding", "UTF-8")))
        self.terminal_timestamp_var = tk.BooleanVar(value=False)
        self.terminal_autoscroll_var = tk.BooleanVar(value=True)
        self.terminal_pause_var = tk.BooleanVar(value=False)
        self.terminal_local_echo_var = tk.BooleanVar(value=False)
        self.terminal_send_mode_var = tk.StringVar(value="Text")
        self.terminal_line_ending_var = tk.StringVar(value=str(self.settings.get("terminal_line_ending", "None")))
        self.terminal_send_var = tk.StringVar()
        self.terminal_preset_var = tk.StringVar(value="Custom")
        self.terminal_advanced_visible = False

        self.terminal_tab.columnconfigure(0, weight=1)
        self.terminal_tab.rowconfigure(2, weight=1)

        connection = ttk.LabelFrame(self.terminal_tab, text="Connection", padding=7)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        connection.columnconfigure(0, weight=1)
        ttk.Label(connection, text="Serial port").grid(row=0, column=0, sticky="w")
        self.terminal_port_combo = ttk.Combobox(connection, textvariable=self.terminal_port_var, width=26)
        self.terminal_port_combo.grid(row=1, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(connection, text="Refresh", command=self._refresh_terminal_ports).grid(row=1, column=1, padx=(0, 10))
        ttk.Label(connection, text="Preset").grid(row=0, column=2, sticky="w")
        preset_combo = ttk.Combobox(
            connection,
            textvariable=self.terminal_preset_var,
            state="readonly",
            width=20,
            values=(
                "Custom",
                "ATtiny software UART 2400 8N1",
                "Common serial 9600 8N1",
                "Arduino / ESP 115200 8N1",
            ),
        )
        preset_combo.grid(row=1, column=2, padx=(0, 8))
        preset_combo.bind("<<ComboboxSelected>>", lambda _event: self._terminal_apply_preset())
        ttk.Label(connection, text="Baud").grid(row=0, column=3, sticky="w")
        ttk.Combobox(
            connection,
            textvariable=self.terminal_baud_var,
            width=10,
            values=("300", "600", "1200", "2400", "4800", "9600", "14400", "19200", "28800", "38400", "57600", "115200", "230400"),
        ).grid(row=1, column=3, padx=(0, 8))
        self.terminal_connect_button = ttk.Button(connection, text="Connect", command=self._toggle_terminal_connection, width=11)
        self.terminal_connect_button.grid(row=1, column=4, padx=(0, 6))

        status = ttk.Frame(connection)
        status.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        ttk.Label(status, textvariable=self.terminal_status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(status, textvariable=self.terminal_modem_var, foreground="#555555").pack(side="left", padx=(16, 0))
        ttk.Button(status, text="Wiring", command=self._terminal_help).pack(side="right")
        self.terminal_advanced_button = ttk.Button(status, text="Show advanced", command=self._toggle_terminal_advanced)
        self.terminal_advanced_button.pack(side="right", padx=(0, 6))
        ttk.Label(status, textvariable=self.terminal_count_var, foreground="#555555").pack(side="right", padx=(0, 12))

        self.terminal_advanced_frame = ttk.LabelFrame(self.terminal_tab, text="Advanced serial settings", padding=6)
        self.terminal_advanced_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        labels = (("Data bits", 0), ("Parity", 1), ("Stop bits", 2), ("Flow control", 3))
        for label, column in labels:
            ttk.Label(self.terminal_advanced_frame, text=label).grid(row=0, column=column, sticky="w")
        ttk.Combobox(self.terminal_advanced_frame, textvariable=self.terminal_data_bits_var, state="readonly", width=6, values=("5", "6", "7", "8")).grid(row=1, column=0, padx=(0, 6))
        ttk.Combobox(self.terminal_advanced_frame, textvariable=self.terminal_parity_var, state="readonly", width=8, values=("None", "Even", "Odd", "Mark", "Space")).grid(row=1, column=1, padx=(0, 6))
        ttk.Combobox(self.terminal_advanced_frame, textvariable=self.terminal_stop_bits_var, state="readonly", width=6, values=("1", "1.5", "2")).grid(row=1, column=2, padx=(0, 6))
        ttk.Combobox(self.terminal_advanced_frame, textvariable=self.terminal_flow_var, state="readonly", width=11, values=("None", "RTS/CTS", "XON/XOFF", "DSR/DTR")).grid(row=1, column=3, padx=(0, 10))
        ttk.Checkbutton(self.terminal_advanced_frame, text="DTR", variable=self.terminal_dtr_var, command=self._terminal_set_dtr).grid(row=1, column=4, padx=(0, 6))
        ttk.Checkbutton(self.terminal_advanced_frame, text="RTS", variable=self.terminal_rts_var, command=self._terminal_set_rts).grid(row=1, column=5, padx=(0, 10))
        ttk.Button(self.terminal_advanced_frame, text="Send break", command=self._terminal_send_break).grid(row=1, column=6, padx=(0, 5))
        ttk.Button(self.terminal_advanced_frame, text="Clear RX", command=self._terminal_clear_input).grid(row=1, column=7, padx=(0, 5))
        ttk.Button(self.terminal_advanced_frame, text="Clear TX", command=self._terminal_clear_output).grid(row=1, column=8)
        self.terminal_advanced_frame.grid_remove()

        receive_box = ttk.LabelFrame(self.terminal_tab, text="Terminal", padding=5)
        self.terminal_receive_box = receive_box
        receive_box.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        receive_box.columnconfigure(0, weight=1)
        receive_box.rowconfigure(1, weight=1)
        receive_toolbar = ttk.Frame(receive_box)
        receive_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(receive_toolbar, text="View").pack(side="left")
        ttk.Combobox(receive_toolbar, textvariable=self.terminal_display_var, state="readonly", width=11, values=("Text", "Hex", "Text + Hex")).pack(side="left", padx=(4, 8))
        ttk.Label(receive_toolbar, text="Encoding").pack(side="left")
        ttk.Combobox(receive_toolbar, textvariable=self.terminal_encoding_var, state="readonly", width=9, values=("UTF-8", "ASCII", "Latin-1")).pack(side="left", padx=(4, 8))
        ttk.Checkbutton(receive_toolbar, text="Timestamps", variable=self.terminal_timestamp_var).pack(side="left")
        ttk.Checkbutton(receive_toolbar, text="Auto-scroll", variable=self.terminal_autoscroll_var).pack(side="left", padx=(7, 0))
        ttk.Checkbutton(receive_toolbar, text="Pause", variable=self.terminal_pause_var).pack(side="left", padx=(7, 0))
        ttk.Button(receive_toolbar, text="Clear", command=self._terminal_clear_display).pack(side="right")
        ttk.Button(receive_toolbar, text="Save capture", command=self._terminal_save_capture).pack(side="right", padx=(0, 5))

        text_frame = ttk.Frame(receive_box)
        text_frame.grid(row=1, column=0, sticky="nsew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.terminal_text = tk.Text(text_frame, font=("Consolas", 9), wrap="none", height=8)
        terminal_y = ttk.Scrollbar(text_frame, orient="vertical", command=self.terminal_text.yview)
        terminal_x = ttk.Scrollbar(text_frame, orient="horizontal", command=self.terminal_text.xview)
        self.terminal_text.configure(yscrollcommand=terminal_y.set, xscrollcommand=terminal_x.set)
        self.terminal_text.grid(row=0, column=0, sticky="nsew")
        terminal_y.grid(row=0, column=1, sticky="ns")
        terminal_x.grid(row=1, column=0, sticky="ew")

        send_box = ttk.LabelFrame(self.terminal_tab, text="Send", padding=6)
        send_box.grid(row=3, column=0, sticky="ew")
        send_box.columnconfigure(0, weight=1)
        self.terminal_send_entry = ttk.Entry(send_box, textvariable=self.terminal_send_var)
        self.terminal_send_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.terminal_send_entry.bind("<Return>", lambda _event: self._terminal_send())
        self.terminal_send_entry.bind("<Up>", self._terminal_history_previous)
        self.terminal_send_entry.bind("<Down>", self._terminal_history_next)
        ttk.Combobox(send_box, textvariable=self.terminal_send_mode_var, state="readonly", width=8, values=("Text", "Hex")).grid(row=0, column=1, padx=(0, 6))
        ttk.Combobox(send_box, textvariable=self.terminal_line_ending_var, state="readonly", width=8, values=("None", "LF", "CR", "CRLF")).grid(row=0, column=2, padx=(0, 6))
        ttk.Checkbutton(send_box, text="Local echo", variable=self.terminal_local_echo_var).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(send_box, text="Send", command=self._terminal_send, width=9).grid(row=0, column=4, padx=(0, 5))
        ttk.Button(send_box, text="Send file…", command=self._terminal_send_file).grid(row=0, column=5)
        ttk.Label(
            send_box,
            text="Enter sends; Up/Down recalls commands. Hex example: 48 65 6C 6C 6F. Use loop:// for a hardware-free test.",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        if not serial_available():
            self.terminal_status_var.set("Serial support unavailable: " + serial_import_error())
            self.terminal_connect_button.configure(state="disabled")
        self._refresh_terminal_ports()
        self.after(500, self._terminal_update_modem_lines)

    def _terminal_apply_preset(self) -> None:
        preset = self.terminal_preset_var.get()
        values = {
            "ATtiny software UART 2400 8N1": ("2400", "8", "None", "1", "None"),
            "Common serial 9600 8N1": ("9600", "8", "None", "1", "None"),
            "Arduino / ESP 115200 8N1": ("115200", "8", "None", "1", "None"),
        }
        if preset not in values:
            return
        baud, data_bits, parity, stop_bits, flow = values[preset]
        self.terminal_baud_var.set(baud)
        self.terminal_data_bits_var.set(data_bits)
        self.terminal_parity_var.set(parity)
        self.terminal_stop_bits_var.set(stop_bits)
        self.terminal_flow_var.set(flow)
        self.status_var.set(f"Terminal preset selected: {preset}")

    def _toggle_terminal_advanced(self) -> None:
        self.terminal_advanced_visible = not self.terminal_advanced_visible
        if self.terminal_advanced_visible:
            self.terminal_advanced_frame.grid()
            self.terminal_advanced_button.configure(text="Hide advanced")
        else:
            self.terminal_advanced_frame.grid_remove()
            self.terminal_advanced_button.configure(text="Show advanced")

    def _terminal_history_previous(self, _event=None):
        if not self.terminal_send_history:
            return "break"
        self.terminal_history_index = max(0, self.terminal_history_index - 1)
        self.terminal_send_var.set(self.terminal_send_history[self.terminal_history_index])
        self.terminal_send_entry.icursor("end")
        return "break"

    def _terminal_history_next(self, _event=None):
        if not self.terminal_send_history:
            return "break"
        self.terminal_history_index = min(len(self.terminal_send_history), self.terminal_history_index + 1)
        if self.terminal_history_index >= len(self.terminal_send_history):
            self.terminal_send_var.set("")
        else:
            self.terminal_send_var.set(self.terminal_send_history[self.terminal_history_index])
        self.terminal_send_entry.icursor("end")
        return "break"

    def _refresh_terminal_ports(self) -> None:
        current = self.terminal_port_var.get().strip()
        self.terminal_port_map = {}
        values: List[str] = []
        for item in list_serial_ports():
            values.append(item.display)
            self.terminal_port_map[item.display] = item.device
        values.append("loop://")
        self.terminal_port_map["loop://"] = "loop://"
        self.terminal_port_combo.configure(values=tuple(values))
        if current:
            self.terminal_port_var.set(current)
        elif values:
            self.terminal_port_var.set(values[0])

    def _terminal_selected_port(self) -> str:
        selected = self.terminal_port_var.get().strip()
        return self.terminal_port_map.get(selected, selected.split(" — ", 1)[0].strip())

    def _terminal_config(self) -> SerialConfig:
        parity_map = {"None": "N", "Even": "E", "Odd": "O", "Mark": "M", "Space": "S"}
        try:
            baud = int(self.terminal_baud_var.get().strip())
        except ValueError as exc:
            raise ValueError("Baud rate must be a whole number.") from exc
        if not 1 <= baud <= 4_000_000:
            raise ValueError("Baud rate must be between 1 and 4,000,000.")
        return SerialConfig(
            port=self._terminal_selected_port(),
            baudrate=baud,
            bytesize=int(self.terminal_data_bits_var.get()),
            parity=parity_map[self.terminal_parity_var.get()],
            stopbits=float(self.terminal_stop_bits_var.get()),
            flow_control=self.terminal_flow_var.get(),
            dtr=bool(self.terminal_dtr_var.get()),
            rts=bool(self.terminal_rts_var.get()),
        )

    def _toggle_terminal_connection(self) -> None:
        if self.serial_session.connected:
            self.serial_session.disconnect()
            self.terminal_connect_button.configure(text="Connect")
            return
        try:
            config = self._terminal_config()
            self.serial_session.connect(config)
            self.settings.values.update({
                "terminal_port": config.port,
                "terminal_baud": str(config.baudrate),
                "terminal_data_bits": str(config.bytesize),
                "terminal_parity": self.terminal_parity_var.get(),
                "terminal_stop_bits": self.terminal_stop_bits_var.get(),
                "terminal_flow_control": self.terminal_flow_var.get(),
                "terminal_display": self.terminal_display_var.get(),
                "terminal_encoding": self.terminal_encoding_var.get(),
                "terminal_line_ending": self.terminal_line_ending_var.get(),
            })
            self.settings.save()
            self.terminal_connect_button.configure(text="Disconnect")
            self.terminal_send_entry.focus_set()
            self._append_log(f"Serial terminal connected: {config.port} {config.baudrate} {config.bytesize}{config.parity}{config.stopbits}")
        except Exception as exc:
            messagebox.showerror("Serial connection failed", str(exc), parent=self)

    def _terminal_set_status(self, status: str) -> None:
        self.terminal_status_var.set(status)
        if not self.serial_session.connected:
            self.terminal_connect_button.configure(text="Connect")

    def _terminal_connection_error(self, error: str) -> None:
        self.serial_session.disconnect()
        self.terminal_connect_button.configure(text="Connect")
        self.terminal_status_var.set("Connection error")
        self._append_log(f"Serial terminal error: {error}")
        messagebox.showerror("Serial terminal error", error, parent=self)

    def _terminal_receive(self, data: bytes) -> None:
        self.terminal_capture.extend(data)
        self.terminal_rx_bytes += len(data)
        self._terminal_update_counts()
        if self.terminal_pause_var.get():
            return
        self._terminal_append_data(data, direction="RX")

    def _terminal_append_data(self, data: bytes, direction: str = "RX") -> None:
        if not data:
            return
        display = self.terminal_display_var.get()
        encoding_name = {"UTF-8": "utf-8", "ASCII": "ascii", "Latin-1": "latin-1"}[self.terminal_encoding_var.get()]
        prefix = ""
        if self.terminal_timestamp_var.get():
            prefix = datetime.now().strftime("[%H:%M:%S.%f]")[:-3] + f" {direction} "
        elif display == "Text + Hex" or direction == "TX":
            prefix = f"{direction} "
        if display == "Hex":
            rendered = prefix + format_hex(data) + "\n"
        elif display == "Text + Hex":
            rendered = prefix + format_text_and_hex(data, encoding_name) + "\n"
        else:
            rendered = prefix + data.decode(encoding_name, errors="replace")
            if self.terminal_timestamp_var.get() and not rendered.endswith(("\n", "\r")):
                rendered += "\n"
        self.terminal_text.insert("end", rendered)
        if self.terminal_autoscroll_var.get():
            self.terminal_text.see("end")

    def _terminal_line_ending(self) -> bytes:
        return {"None": b"", "LF": b"\n", "CR": b"\r", "CRLF": b"\r\n"}[self.terminal_line_ending_var.get()]

    def _terminal_send(self) -> None:
        try:
            entered = self.terminal_send_var.get()
            if self.terminal_send_mode_var.get() == "Hex":
                data = parse_hex_bytes(entered)
            else:
                data = entered.encode(
                    {"UTF-8": "utf-8", "ASCII": "ascii", "Latin-1": "latin-1"}[self.terminal_encoding_var.get()],
                    errors="strict",
                ) + self._terminal_line_ending()
            if not data:
                return
            count = self.serial_session.write(data)
            self.terminal_tx_bytes += count
            self._terminal_update_counts()
            if entered and (not self.terminal_send_history or self.terminal_send_history[-1] != entered):
                self.terminal_send_history.append(entered)
                self.terminal_send_history = self.terminal_send_history[-100:]
            self.terminal_history_index = len(self.terminal_send_history)
            if self.terminal_local_echo_var.get():
                self._terminal_append_data(data, direction="TX")
            self.terminal_send_var.set("")
        except Exception as exc:
            messagebox.showerror("Send failed", str(exc), parent=self)

    def _terminal_send_file(self) -> None:
        path_text = filedialog.askopenfilename(parent=self, title="Select a file to send", filetypes=[("All files", "*.*")])
        if not path_text:
            return
        path = Path(path_text)
        try:
            data = path.read_bytes()
        except OSError as exc:
            messagebox.showerror("Send file", str(exc), parent=self)
            return
        if not self._confirm(
            "Send raw file",
            f"File: {path}\nSize: {len(data)} bytes\nPort: {self._terminal_selected_port()}\n\nThe bytes will be transmitted exactly as stored. This is not an AVR programming operation.",
            button_text="Send file",
        ):
            return
        try:
            count = self.serial_session.write(data)
            self.terminal_tx_bytes += count
            self._terminal_update_counts()
            self._append_log(f"Serial terminal sent file {path} ({count} bytes)")
        except Exception as exc:
            messagebox.showerror("Send file failed", str(exc), parent=self)

    def _terminal_send_break(self) -> None:
        try:
            self.serial_session.send_break()
        except Exception as exc:
            messagebox.showerror("Send break", str(exc), parent=self)

    def _terminal_set_dtr(self) -> None:
        try:
            self.serial_session.set_dtr(self.terminal_dtr_var.get())
        except Exception as exc:
            messagebox.showerror("DTR", str(exc), parent=self)

    def _terminal_set_rts(self) -> None:
        try:
            self.serial_session.set_rts(self.terminal_rts_var.get())
        except Exception as exc:
            messagebox.showerror("RTS", str(exc), parent=self)

    def _terminal_clear_input(self) -> None:
        try:
            self.serial_session.clear_input()
        except Exception as exc:
            messagebox.showerror("Clear RX buffer", str(exc), parent=self)

    def _terminal_clear_output(self) -> None:
        try:
            self.serial_session.clear_output()
        except Exception as exc:
            messagebox.showerror("Clear TX buffer", str(exc), parent=self)

    def _terminal_clear_display(self) -> None:
        self.terminal_text.delete("1.0", "end")
        self.terminal_capture.clear()
        self.terminal_rx_bytes = 0
        self.terminal_tx_bytes = 0
        self._terminal_update_counts()

    def _terminal_save_capture(self) -> None:
        path_text = filedialog.asksaveasfilename(
            parent=self,
            title="Save terminal capture",
            defaultextension=".bin",
            filetypes=[("Raw binary", "*.bin"), ("Text log", "*.txt"), ("All files", "*.*")],
        )
        if not path_text:
            return
        path = Path(path_text)
        try:
            if path.suffix.lower() == ".txt":
                path.write_text(self.terminal_text.get("1.0", "end-1c"), encoding="utf-8")
            else:
                path.write_bytes(bytes(self.terminal_capture))
            messagebox.showinfo("Terminal capture", f"Saved to {path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Terminal capture", str(exc), parent=self)

    def _terminal_update_counts(self) -> None:
        self.terminal_count_var.set(f"RX {self.terminal_rx_bytes} bytes   TX {self.terminal_tx_bytes} bytes")

    def _terminal_update_modem_lines(self) -> None:
        try:
            self.terminal_modem_var.set(self.serial_session.modem_lines())
        finally:
            if self.winfo_exists():
                self.after(500, self._terminal_update_modem_lines)

    def _terminal_help(self) -> None:
        messagebox.showinfo(
            "ATtiny85 terminal wiring and limits",
            "The FabISP/USBtiny programmer is not a serial adapter, and ISP cannot provide a live text terminal.\n\n"
            "For two-way runtime communication, connect a separate USB-to-TTL serial adapter and run firmware on the ATtiny85 that implements a software UART. The ATtiny85 has no hardware UART.\n\n"
            "Typical wiring:\n"
            "• USB-TTL TX -> the ATtiny85 firmware's software-RX pin\n"
            "• USB-TTL RX <- the firmware's software-TX pin\n"
            "• USB-TTL GND <-> ATtiny85 GND\n"
            "• Use compatible 3.3 V or 5 V logic levels\n\n"
            "Do not connect two different power outputs together. Normally power the target from one source only. Disconnect or electrically isolate a serial adapter that drives an ISP pin before programming.\n\n"
            "Use loop:// as the port to test the terminal internally: everything sent is returned immediately without hardware.",
            parent=self,
        )

    def _on_close(self) -> None:
        try:
            layout_values = {"window_geometry": self.geometry()}
            for memory in ("flash", "eeprom"):
                dashboard = getattr(self, f"{memory}_dashboard_pane", None)
                workspace = getattr(self, f"{memory}_workspace_pane", None)
                if dashboard is not None and len(dashboard.panes()) > 1:
                    try:
                        layout_values[f"{memory}_dashboard_split"] = int(dashboard.sashpos(0))
                    except (tk.TclError, ValueError):
                        pass
                if workspace is not None and len(workspace.panes()) > 1:
                    try:
                        layout_values[f"{memory}_workspace_split"] = int(workspace.sashpos(0))
                    except (tk.TclError, ValueError):
                        pass
            self.settings.values.update(layout_values)
            self.settings.values.update({
                "terminal_port": self._terminal_selected_port() if hasattr(self, "terminal_port_var") else "",
                "terminal_baud": self.terminal_baud_var.get() if hasattr(self, "terminal_baud_var") else "9600",
                "terminal_data_bits": self.terminal_data_bits_var.get() if hasattr(self, "terminal_data_bits_var") else "8",
                "terminal_parity": self.terminal_parity_var.get() if hasattr(self, "terminal_parity_var") else "None",
                "terminal_stop_bits": self.terminal_stop_bits_var.get() if hasattr(self, "terminal_stop_bits_var") else "1",
                "terminal_flow_control": self.terminal_flow_var.get() if hasattr(self, "terminal_flow_var") else "None",
                "terminal_display": self.terminal_display_var.get() if hasattr(self, "terminal_display_var") else "Text",
                "terminal_encoding": self.terminal_encoding_var.get() if hasattr(self, "terminal_encoding_var") else "UTF-8",
                "terminal_line_ending": self.terminal_line_ending_var.get() if hasattr(self, "terminal_line_ending_var") else "None",
            })
            self.settings.save()
            self.serial_session.disconnect()
        finally:
            self.destroy()

    # ---------- logs and information ----------

    def _build_logs_tab(self) -> None:
        self.log_show_details_var = tk.BooleanVar(value=False)
        toolbar = ttk.Frame(self.logs_tab)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="Clear view", command=self._clear_logs).pack(side="left")
        ttk.Button(toolbar, text="Copy last AVRDUDE command", command=self._copy_last_command).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            toolbar,
            text="Show detailed AVRDUDE output",
            variable=self.log_show_details_var,
            command=self._refresh_log_view,
        ).pack(side="left", padx=(12, 0))
        ttk.Label(toolbar, text=f"Session log: {self.log_path.name}", foreground="#555555").pack(side="right")
        ttk.Label(
            self.logs_tab,
            text="The on-screen log defaults to important events only. The complete unfiltered output is always retained in the session log file.",
            foreground="#555555",
        ).pack(fill="x", pady=(0, 5))
        frame = ttk.Frame(self.logs_tab)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, font=("Consolas", 9), wrap="none", state="disabled", height=14)
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _refresh_log_view(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for line in self.log_history:
            raw = line.split("] ", 1)[1] if "] " in line else line
            if self.log_show_details_var.get() or self._is_summary_log(raw):
                self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _copy_last_command(self) -> None:
        if not self.last_avrdude_command:
            messagebox.showinfo("AVRDUDE command", "No AVRDUDE command has run in this session.", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(self.last_avrdude_command)
        self.update()
        self.status_var.set("Last AVRDUDE command copied")

    def _clear_logs(self) -> None:
        self.log_history = []
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.status_var.set("On-screen log cleared; the session log file remains unchanged")

    def _build_info_tab(self) -> None:
        text = tk.Text(self.info_tab, wrap="word")
        scroll = ttk.Scrollbar(self.info_tab, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.info_tab.columnconfigure(0, weight=1)
        self.info_tab.rowconfigure(0, weight=1)
        info = f"""
ATtiny85 Explorer v{__version__}

PURPOSE
Portable ATtiny85 programming, memory inspection, editing, fuse management, backup/restore, diagnostics, and serial-terminal utility. It works with finished HEX and BIN files; it does not compile sketches or reconstruct source code from a chip.

SUPPORTED TARGET
Device: ATtiny85
Expected signature: 0x1E930B
Part ID: t85
Flash: 8192 bytes (64-byte pages)
EEPROM: 512 bytes (4-byte pages)
SRAM: 512 bytes

PROGRAMMER AND BACKEND
Default programmer: FabISP / USBtinyISP
Programmer ID: usbtiny
Built-in backend: AVRDUDE 6.3-20190619
The normal release is one self-contained EXE. Arduino IDE, Python, and a separate AVRDUDE installation are not required.
Windows still requires a working USB driver for the connected programmer.

SAFETY MODES
Basic: routine programming, memory viewing/editing, backups, verification, and guided internal-clock presets.
Advanced: all documented clock choices, fuse features, offsets, preservation controls, and alternate buffer formats.
Expert: lock bits, destructive unlock erase, raw fuse bytes, and manual AVRDUDE arguments. Critical actions require explicit confirmation.

IMPORTANT HARDWARE LIMITS
USBtiny does not report target voltage digitally; measure VCC with a meter.
RSTDISBL ends normal ISP access and requires high-voltage serial programming for recovery.
External-clock or crystal fuse settings require the matching clock hardware before programming.
Lock modes 2 and 3 restrict programming; only chip erase clears lock bits.

FILES AND COMPATIBILITY
Existing .avrxproj projects and .avrxpkg backups from earlier releases remain supported.
Settings, logs, automatic backups, and project defaults are stored under AppData\\Roaming\\ATtiny85 Explorer for the current Windows user.
The application supports fully updated Windows 7 SP1 through Windows 11 on compatible Intel/AMD systems. Windows 7 requires Microsoft update KB2533623.

PROGRAMMING BEHAVIOR
Before a write, ATtiny85 Explorer verifies the device signature, applies the selected backup and verification options, validates the resolved image, and uses exact readback checks where requested. Failed writes are inspected before a retry is offered.

DEVICE TERMINAL
The terminal uses ordinary Windows COM ports or its loop:// self-test. USBtiny/FabISP is a programming interface, not a serial bridge; live serial communication requires separate USB-TTL hardware and suitable ATtiny85 firmware.
""".strip()
        text.insert("1.0", info)
        text.configure(state="disabled")

    # ---------- shared file tools ----------

    @staticmethod
    def _set_text(widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _clear_selected_file(self, memory: str) -> None:
        self._new_blank_editor(memory)

    def _view_selected_image(self, memory: str) -> None:
        inspected = self._inspect_flash() if memory == "flash" else self._inspect_eeprom()
        if inspected:
            self.notebook.select(self.flash_tab if memory == "flash" else self.eeprom_tab)
            self._select_memory_view(memory, "file")

def run() -> None:
    app = AVRExplorerApp()
    app.mainloop()
