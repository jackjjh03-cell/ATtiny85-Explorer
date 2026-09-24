/*
 * ATtiny85 Explorer ATtiny85 temporary on-chip self-test.
 * Built as a freestanding image. It does not use external pins or libraries.
 * The host application backs up the complete chip before loading this image,
 * reads the 32-byte report from EEPROM 0x01E0-0x01FF, then restores the backup.
 */
typedef unsigned char uint8_t;
typedef unsigned short uint16_t;

#define IO8(address) (*(volatile uint8_t *)(0x20u + (address)))
#define ADCSRA IO8(0x06)
#define ADMUX  IO8(0x07)
#define EECR   IO8(0x1C)
#define EEDR   IO8(0x1D)
#define EEARL  IO8(0x1E)
#define EEARH  IO8(0x1F)
#define PRR    IO8(0x20)
#define PLLCSR IO8(0x27)
#define OCR1C  IO8(0x2D)
#define TCNT1  IO8(0x2F)
#define TCCR1  IO8(0x30)
#define OSCCAL IO8(0x31)
#define TCNT0  IO8(0x32)
#define TCCR0B IO8(0x33)
#define MCUSR  IO8(0x34)
#define TIFR   IO8(0x38)

#define REPORT_BASE 0x01E0u
#define REPORT_SIZE 32u
#define SRAM_FIRST  0x0060u
#define SRAM_LAST   0x01FFu
#define EEPROM_TEST_ADDRESS 0x01DFu

static void eeprom_wait(void) {
    while (EECR & (1u << 1)) { }
}

static uint8_t eeprom_read_byte(uint16_t address) {
    eeprom_wait();
    EEARL = (uint8_t)address;
    EEARH = (uint8_t)(address >> 8);
    __asm__ __volatile__("sbi 0x1c, 0" ::: "memory");
    return EEDR;
}

static void eeprom_write_byte(uint16_t address, uint8_t value) {
    eeprom_wait();
    EEARL = (uint8_t)address;
    EEARH = (uint8_t)(address >> 8);
    EEDR = value;
    /* Atomic EEPROM master-enable/program-enable sequence. */
    __asm__ __volatile__(
        "cli\n\t"
        "sbi 0x1c, 2\n\t"
        "sbi 0x1c, 1\n\t"
        ::: "memory"
    );
}

static uint8_t cpu_test(void) {
    volatile uint8_t a = 0x55u;
    volatile uint8_t b = 0x0Fu;
    uint8_t ok = 1u;
    if ((uint8_t)(a + b) != 0x64u) ok = 0u;
    if ((uint8_t)(a ^ b) != 0x5Au) ok = 0u;
    if ((uint8_t)(a & b) != 0x05u) ok = 0u;
    if ((uint8_t)(a | b) != 0x5Fu) ok = 0u;
    if ((uint8_t)(a << 1) != 0xAAu) ok = 0u;
    if ((uint8_t)(a >> 1) != 0x2Au) ok = 0u;
    return ok;
}

static uint8_t sram_pattern(uint8_t pattern, uint16_t *failed_address, uint8_t *actual) {
    volatile uint8_t *p;
    for (p = (volatile uint8_t *)SRAM_FIRST; p <= (volatile uint8_t *)SRAM_LAST; ++p) {
        *p = pattern;
    }
    for (p = (volatile uint8_t *)SRAM_FIRST; p <= (volatile uint8_t *)SRAM_LAST; ++p) {
        uint8_t value = *p;
        if (value != pattern) {
            *failed_address = (uint16_t)p;
            *actual = value;
            return 0u;
        }
    }
    return 1u;
}

static uint8_t sram_test(uint16_t *failed_address, uint8_t *expected, uint8_t *actual) {
    const uint8_t patterns[4] = {0x00u, 0xFFu, 0xAAu, 0x55u};
    uint8_t i;
    *failed_address = 0xFFFFu;
    *expected = 0u;
    *actual = 0u;
    for (i = 0u; i < 4u; ++i) {
        *expected = patterns[i];
        if (!sram_pattern(patterns[i], failed_address, actual)) return 0u;
    }
    return 1u;
}

static uint8_t timer0_test(void) {
    uint16_t guard = 0xFFFFu;
    PRR &= (uint8_t)~(1u << 2);
    TCCR0B = 0u;
    TCNT0 = 0u;
    TIFR = (1u << 1);
    TCCR0B = 1u;
    while (!(TIFR & (1u << 1)) && --guard) { }
    TCCR0B = 0u;
    return (TIFR & (1u << 1)) ? 1u : 0u;
}

static uint8_t timer1_test(void) {
    uint16_t guard = 0xFFFFu;
    PRR &= (uint8_t)~(1u << 3);
    TCCR1 = 0u;
    OCR1C = 0xFFu;
    TCNT1 = 0u;
    TIFR = (1u << 2);
    TCCR1 = 1u;
    while (!(TIFR & (1u << 2)) && --guard) { }
    TCCR1 = 0u;
    return (TIFR & (1u << 2)) ? 1u : 0u;
}

static uint8_t eeprom_runtime_test(uint8_t *original_value) {
    uint8_t ok = 1u;
    *original_value = eeprom_read_byte(EEPROM_TEST_ADDRESS);
    eeprom_write_byte(EEPROM_TEST_ADDRESS, 0x00u);
    if (eeprom_read_byte(EEPROM_TEST_ADDRESS) != 0x00u) ok = 0u;
    eeprom_write_byte(EEPROM_TEST_ADDRESS, 0xFFu);
    if (eeprom_read_byte(EEPROM_TEST_ADDRESS) != 0xFFu) ok = 0u;
    eeprom_write_byte(EEPROM_TEST_ADDRESS, 0xA5u);
    if (eeprom_read_byte(EEPROM_TEST_ADDRESS) != 0xA5u) ok = 0u;
    eeprom_write_byte(EEPROM_TEST_ADDRESS, *original_value);
    if (eeprom_read_byte(EEPROM_TEST_ADDRESS) != *original_value) ok = 0u;
    return ok;
}

static void write_report(const uint8_t *report) {
    uint8_t i;
    for (i = 0u; i < REPORT_SIZE; ++i) {
        eeprom_write_byte((uint16_t)(REPORT_BASE + i), report[i]);
    }
}

__attribute__((noinline)) void main_test(void) {
    uint8_t report[REPORT_SIZE];
    uint8_t i;
    uint16_t failed_address;
    uint8_t expected;
    uint8_t actual;
    uint8_t original_eeprom;
    uint8_t cpu_ok;
    uint8_t sram_ok;
    uint8_t timer0_ok;
    uint8_t timer1_ok;
    uint8_t eeprom_ok;
    uint8_t reset_flags = MCUSR;

    MCUSR = 0u;
    for (i = 0u; i < REPORT_SIZE; ++i) report[i] = 0u;

    cpu_ok = cpu_test();
    sram_ok = sram_test(&failed_address, &expected, &actual);
    timer0_ok = timer0_test();
    timer1_ok = timer1_test();
    eeprom_ok = eeprom_runtime_test(&original_eeprom);

    report[0] = 'A'; report[1] = 'V'; report[2] = 'R'; report[3] = 'X';
    report[4] = 1u;
    report[5] = cpu_ok;
    report[6] = sram_ok;
    report[7] = timer0_ok;
    report[8] = timer1_ok;
    report[9] = eeprom_ok;
    report[10] = (uint8_t)(cpu_ok && sram_ok && timer0_ok && timer1_ok && eeprom_ok);
    report[11] = 0xA5u;
    report[12] = reset_flags;
    report[13] = OSCCAL;
    report[14] = (uint8_t)((SRAM_LAST - SRAM_FIRST + 1u) & 0xFFu);
    report[15] = (uint8_t)((SRAM_LAST - SRAM_FIRST + 1u) >> 8);
    report[16] = (uint8_t)failed_address;
    report[17] = (uint8_t)(failed_address >> 8);
    report[18] = expected;
    report[19] = actual;
    report[20] = original_eeprom;
    report[21] = PLLCSR;
    report[22] = PRR;
    report[23] = 0x48u;
    report[24] = 0x57u;
    report[25] = 0x31u;
    report[26] = 0x00u;
    report[27] = 0x00u;
    report[28] = 0x00u;
    report[29] = 0x00u;
    report[30] = 0x00u;
    report[31] = 0x5Au;
    write_report(report);
    for (;;) { __asm__ __volatile__("nop"); }
}

__attribute__((naked, used, section(".text.start"))) void _start(void) {
    __asm__ __volatile__(
        "ldi r16, 0x5f\n\t"
        "out 0x3d, r16\n\t"
        "ldi r16, 0x02\n\t"
        "out 0x3e, r16\n\t"
        "clr r1\n\t"
        "rcall main_test\n\t"
        "1: rjmp 1b\n\t"
    );
}
