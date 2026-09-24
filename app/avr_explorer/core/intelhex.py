from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .models import ImageInfo


class IntelHexError(ValueError):
    pass


def _checksum_ok(record: bytes) -> bool:
    return (sum(record) & 0xFF) == 0


def parse_intel_hex(path: Path) -> Dict[int, int]:
    return parse_intel_hex_text(path.read_text(encoding="ascii"))


def parse_intel_hex_text(text: str) -> Dict[int, int]:
    memory: Dict[int, int] = {}
    upper_linear = 0
    upper_segment = 0
    eof_seen = False

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise IntelHexError(f"Line {line_number}: missing ':'")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise IntelHexError(f"Line {line_number}: invalid hexadecimal data") from exc
        if len(record) < 5:
            raise IntelHexError(f"Line {line_number}: record is too short")
        length = record[0]
        if len(record) != length + 5:
            raise IntelHexError(f"Line {line_number}: byte count does not match the record length")
        if not _checksum_ok(record):
            raise IntelHexError(f"Line {line_number}: checksum failure")

        address = (record[1] << 8) | record[2]
        record_type = record[3]
        data = record[4:4 + length]

        if record_type == 0x00:
            base = upper_linear + upper_segment + address
            for index, value in enumerate(data):
                absolute = base + index
                if absolute in memory and memory[absolute] != value:
                    raise IntelHexError(f"Line {line_number}: conflicting data at address 0x{absolute:04X}")
                memory[absolute] = value
        elif record_type == 0x01:
            eof_seen = True
            break
        elif record_type == 0x02:
            if length != 2:
                raise IntelHexError(f"Line {line_number}: invalid extended segment record")
            upper_segment = int.from_bytes(data, "big") << 4
            upper_linear = 0
        elif record_type == 0x04:
            if length != 2:
                raise IntelHexError(f"Line {line_number}: invalid extended linear record")
            upper_linear = int.from_bytes(data, "big") << 16
            upper_segment = 0
        elif record_type in (0x03, 0x05):
            continue
        else:
            raise IntelHexError(f"Line {line_number}: unsupported record type 0x{record_type:02X}")

    if not eof_seen:
        raise IntelHexError("Intel HEX file has no end-of-file record")
    return memory


def _record(address: int, record_type: int, data: bytes) -> str:
    body = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, record_type]) + data
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


def write_intel_hex(memory: Dict[int, int], path: Path, line_length: int = 16) -> None:
    path.write_text(write_intel_hex_text(memory, line_length=line_length), encoding="ascii")


def write_intel_hex_text(memory: Dict[int, int], line_length: int = 16) -> str:
    if line_length <= 0 or line_length > 255:
        raise ValueError("Invalid Intel HEX line length")
    lines: List[str] = []
    addresses = sorted(memory)
    current_upper = None
    index = 0

    while index < len(addresses):
        start = addresses[index]
        upper = start >> 16
        if upper != current_upper:
            lines.append(_record(0, 0x04, upper.to_bytes(2, "big")))
            current_upper = upper

        low = start & 0xFFFF
        chunk = bytearray()
        expected = start
        while index < len(addresses) and len(chunk) < line_length:
            address = addresses[index]
            if address != expected or (address >> 16) != upper:
                break
            chunk.append(memory[address])
            expected += 1
            index += 1
        lines.append(_record(low, 0x00, bytes(chunk)))

    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


def bytes_to_memory(data: bytes, offset: int = 0, include_ff: bool = True) -> Dict[int, int]:
    if offset < 0:
        raise ValueError("Offset cannot be negative")
    if include_ff:
        return {offset + i: value for i, value in enumerate(data)}
    return {offset + i: value for i, value in enumerate(data) if value != 0xFF}


def memory_to_bytes(memory: Dict[int, int], size: int, fill: int = 0xFF) -> bytes:
    if size < 0:
        raise ValueError("Size cannot be negative")
    result = bytearray([fill] * size)
    for address, value in memory.items():
        if not 0 <= address < size:
            raise ValueError(f"Address 0x{address:X} is outside the memory size")
        result[address] = value
    return bytes(result)


def contiguous_ranges(addresses: Iterable[int]) -> List[List[int]]:
    sorted_addresses = sorted(set(addresses))
    if not sorted_addresses:
        return []
    ranges: List[List[int]] = []
    start = previous = sorted_addresses[0]
    for address in sorted_addresses[1:]:
        if address != previous + 1:
            ranges.append([start, previous])
            start = address
        previous = address
    ranges.append([start, previous])
    return ranges


def inspect_image(path: Path, offset: int = 0) -> ImageInfo:
    suffix = path.suffix.lower()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if suffix in (".hex", ".ihx"):
        memory = parse_intel_hex(path)
        addresses = sorted(memory)
        if addresses:
            minimum, maximum = addresses[0], addresses[-1]
        else:
            minimum = maximum = 0
        return ImageInfo(
            file_type="Intel HEX",
            path=str(path),
            minimum_address=minimum,
            maximum_address=maximum,
            occupied_bytes=len(memory),
            ranges=contiguous_ranges(addresses),
            checksum_valid=True,
            sha256=digest,
            notes=[] if memory else ["The HEX file contains no data bytes."],
        )
    data = path.read_bytes()
    maximum = offset + len(data) - 1 if data else offset
    return ImageInfo(
        file_type="Raw binary",
        path=str(path),
        minimum_address=offset,
        maximum_address=maximum,
        occupied_bytes=len(data),
        ranges=[[offset, maximum]] if data else [],
        checksum_valid=True,
        sha256=digest,
        notes=[] if data else ["The binary file is empty."],
    )


def load_image(path: Path, memory_size: int, offset: int = 0, base: bytes = b"") -> Tuple[bytes, Dict[int, int]]:
    if memory_size <= 0:
        raise ValueError("Memory size must be positive")
    image = bytearray(base if base else bytes([0xFF]) * memory_size)
    if len(image) != memory_size:
        raise ValueError("Base image length does not match the memory size")

    suffix = path.suffix.lower()
    if suffix in (".hex", ".ihx"):
        payload = parse_intel_hex(path)
    else:
        payload = bytes_to_memory(path.read_bytes(), offset)

    if not payload:
        raise ValueError("The selected file contains no data.")
    for address, value in payload.items():
        if not 0 <= address < memory_size:
            raise ValueError(
                f"File data at address 0x{address:X} exceeds memory limit 0x{memory_size - 1:X}."
            )
        image[address] = value
    return bytes(image), payload


def compare_payload(actual: bytes, payload: Dict[int, int]) -> List[Tuple[int, int, int]]:
    mismatches: List[Tuple[int, int, int]] = []
    for address, expected in payload.items():
        if address >= len(actual):
            mismatches.append((address, expected, -1))
        elif actual[address] != expected:
            mismatches.append((address, expected, actual[address]))
    return mismatches
