OUTPUT_FORMAT("elf32-avr")
ENTRY(_start)
SECTIONS
{
  .text 0x0000 :
  {
    KEEP(*(.text.start))
    *(.text)
    *(.text.*)
    *(.rodata)
    *(.rodata.*)
  }
  /DISCARD/ : { *(.comment) *(.note*) }
}
