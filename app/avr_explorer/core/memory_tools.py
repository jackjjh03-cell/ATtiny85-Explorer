from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .intelhex import bytes_to_memory, contiguous_ranges, memory_to_bytes, parse_intel_hex_text, write_intel_hex_text


@dataclass
class MemoryStats:
    capacity: int
    programmed_bytes: int
    erased_bytes: int
    used_percent: float
    first_programmed: Optional[int]
    last_programmed: Optional[int]
    occupied_span: int
    programmed_pages: int
    total_pages: int
    zero_bytes: int
    printable_bytes: int
    sha256: str
    programmed_ranges: List[List[int]]
    largest_erased_start: Optional[int]
    largest_erased_end: Optional[int]
    largest_erased_length: int


def analyze_memory(data: bytes, page_size: int) -> MemoryStats:
    if page_size <= 0:
        raise ValueError("Page size must be positive")
    capacity = len(data)
    programmed = [index for index, value in enumerate(data) if value != 0xFF]
    ranges = contiguous_ranges(programmed)
    first = programmed[0] if programmed else None
    last = programmed[-1] if programmed else None
    span = 0 if first is None or last is None else last - first + 1
    pages = sorted(set(index // page_size for index in programmed))

    largest_start: Optional[int] = None
    largest_end: Optional[int] = None
    largest_length = 0
    run_start: Optional[int] = None
    for index, value in enumerate(data):
        if value == 0xFF and run_start is None:
            run_start = index
        if value != 0xFF and run_start is not None:
            length = index - run_start
            if length > largest_length:
                largest_start = run_start
                largest_end = index - 1
                largest_length = length
            run_start = None
    if run_start is not None:
        length = capacity - run_start
        if length > largest_length:
            largest_start = run_start
            largest_end = capacity - 1
            largest_length = length

    programmed_count = len(programmed)
    return MemoryStats(
        capacity=capacity,
        programmed_bytes=programmed_count,
        erased_bytes=capacity - programmed_count,
        used_percent=(programmed_count / capacity * 100.0) if capacity else 0.0,
        first_programmed=first,
        last_programmed=last,
        occupied_span=span,
        programmed_pages=len(pages),
        total_pages=(capacity + page_size - 1) // page_size,
        zero_bytes=sum(1 for value in data if value == 0x00),
        printable_bytes=sum(1 for value in data if 32 <= value < 127),
        sha256=hashlib.sha256(data).hexdigest(),
        programmed_ranges=ranges,
        largest_erased_start=largest_start,
        largest_erased_end=largest_end,
        largest_erased_length=largest_length,
    )


def format_addressed_hex(data: bytes, bytes_per_line: int = 16) -> str:
    if bytes_per_line <= 0:
        raise ValueError("Bytes per line must be positive")
    lines: List[str] = []
    for address in range(0, len(data), bytes_per_line):
        chunk = data[address:address + bytes_per_line]
        hex_text = " ".join(f"{value:02X}" for value in chunk)
        ascii_text = "".join(chr(value) if 32 <= value < 127 else "." for value in chunk)
        lines.append(f"{address:04X}: {hex_text:<47}  |{ascii_text}|")
    return "\n".join(lines) + ("\n" if lines else "")


def _strip_ascii_column(line: str) -> str:
    if "|" in line:
        return line.split("|", 1)[0].rstrip()
    return line


def parse_addressed_hex(text: str, capacity: int, fill: int = 0xFF) -> Tuple[bytes, int]:
    if capacity <= 0:
        raise ValueError("Capacity must be positive")
    image = bytearray([fill] * capacity)
    written: Dict[int, int] = {}
    sequential_address = 0

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_ascii_column(raw_line)
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue

        address = sequential_address
        payload_text = line
        if ":" in line:
            address_text, payload_text = line.split(":", 1)
            address_text = address_text.strip()
            if not re.fullmatch(r"(?:0x)?[0-9A-Fa-f]+", address_text):
                raise ValueError(f"Line {line_number}: invalid address '{address_text}'")
            address = int(address_text, 16)

        tokens = [token for token in re.split(r"[\s,]+", payload_text.strip()) if token]
        for token in tokens:
            normalized = token[2:] if token.lower().startswith("0x") else token
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", normalized):
                raise ValueError(f"Line {line_number}: invalid byte '{token}'")
            if not 0 <= address < capacity:
                raise ValueError(
                    f"Line {line_number}: address 0x{address:X} exceeds memory limit 0x{capacity - 1:X}"
                )
            value = int(normalized, 16)
            if address in written and written[address] != value:
                raise ValueError(f"Line {line_number}: conflicting byte at address 0x{address:04X}")
            written[address] = value
            image[address] = value
            address += 1
        sequential_address = address

    if not written:
        raise ValueError("The editor contains no hexadecimal bytes.")
    return bytes(image), len(written)


def editor_text_from_memory(data: bytes, format_name: str) -> str:
    normalized = format_name.strip().lower()
    if normalized == "addressed hex":
        return format_addressed_hex(data)
    if normalized == "intel hex":
        return write_intel_hex_text(bytes_to_memory(data, include_ff=True))
    if normalized == "raw hex bytes":
        return "\n".join(
            " ".join(f"{value:02X}" for value in data[offset:offset + 16])
            for offset in range(0, len(data), 16)
        ) + "\n"
    raise ValueError(f"Unsupported editor format: {format_name}")


def memory_from_editor_text(text: str, capacity: int, format_name: str) -> Tuple[bytes, int]:
    normalized = format_name.strip().lower()
    if normalized in ("addressed hex", "raw hex bytes"):
        return parse_addressed_hex(text, capacity)
    if normalized == "intel hex":
        memory = parse_intel_hex_text(text)
        if not memory:
            raise ValueError("The Intel HEX editor contains no data records.")
        return memory_to_bytes(memory, capacity), len(memory)
    raise ValueError(f"Unsupported editor format: {format_name}")


def format_stats(stats: MemoryStats, memory_name: str, page_size: int) -> str:
    first = "--" if stats.first_programmed is None else f"0x{stats.first_programmed:04X}"
    last = "--" if stats.last_programmed is None else f"0x{stats.last_programmed:04X}"
    if stats.largest_erased_start is None:
        erased_region = "none"
    else:
        erased_region = (
            f"0x{stats.largest_erased_start:04X}-0x{stats.largest_erased_end:04X} "
            f"({stats.largest_erased_length:,} bytes)"
        )
    ranges = ", ".join(
        f"0x{start:04X}-0x{end:04X}" for start, end in stats.programmed_ranges[:16]
    ) or "none"
    if len(stats.programmed_ranges) > 16:
        ranges += f", plus {len(stats.programmed_ranges) - 16} more"
    return (
        f"{memory_name} capacity: {stats.capacity:,} bytes ({stats.capacity * 8:,} bits)\n"
        f"Programmed bytes: {stats.programmed_bytes:,} bytes ({stats.used_percent:.2f}%)\n"
        f"Erased/free bytes: {stats.erased_bytes:,} bytes\n"
        f"First programmed address: {first}\n"
        f"Last programmed address: {last}\n"
        f"Occupied address span: {stats.occupied_span:,} bytes\n"
        f"Programmed pages: {stats.programmed_pages:,} of {stats.total_pages:,} pages "
        f"({page_size:,} bytes per page)\n"
        f"Programmed ranges: {ranges}\n"
        f"Largest continuous erased region: {erased_region}\n"
        f"0x00 bytes: {stats.zero_bytes:,} bytes\n"
        f"Printable ASCII bytes: {stats.printable_bytes:,} bytes\n"
        f"SHA-256 of full memory image: {stats.sha256}"
    )
