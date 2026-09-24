ATtiny85 Explorer temporary ATtiny85 self-test

attiny85_selftest.hex is the prebuilt image used by Dashboard > Run on-chip self-test.
The application verifies the image SHA-256 before programming it.

The test uses no external pins. It checks basic CPU arithmetic/control flow,
416 bytes of SRAM with four patterns, Timer0, high-speed Timer1, and runtime
EEPROM read/write. It writes a 32-byte report at EEPROM 0x01E0-0x01FF.

ATtiny85 Explorer creates a complete chip backup first, reads the report, restores the
original Flash and EEPROM, and verifies the restoration byte-for-byte. The source
and linker script are included for auditability; the end user does not need to
compile them.
