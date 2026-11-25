/*
 * ICM-42688-P: the six-axis IMU of the Video Game Module, on the GPIO
 * header's SPI bus
 *
 * On the VGM the IMU's SPI is wired straight to the header: SCK pin 5
 * (PB3), MOSI pin 2 (PA7), MISO pin 3 (PA6), chip select pin 7 (PC3), INT1
 * pin 6 (PB2) and INT2 pin 4 (PA4).  The Flipper's apps for the VGM (Air
 * Mouse, the JS `vgm` module) talk to it from the Flipper, not through the
 * RP2040.  Put it there with `run --spi icm42688p@7`.
 *
 * What is modelled is what those apps use, after the datasheet (DS-000347):
 *
 *   - the SPI frame: an address byte (bit 7: a read), then data, the
 *     address going up by one each byte, except at FIFO_DATA;
 *   - register banks 0 and 4 (REG_BANK_SEL), WHO_AM_I 0x47, the soft reset
 *     of DEVICE_CONFIG;
 *   - the sensors' data registers, at the full scale and in the byte order
 *     (INTF_CONFIG0) that the firmware chose, from the motion that the
 *     properties below hold, with the gyro's user offsets (bank 4,
 *     OFFSET_USER0..4) added;
 *   - the FIFO in stream mode: a 16-byte packet (header, accel, gyro, temp,
 *     timestamp) each output period of the gyro (of the accel when the gyro
 *     is off), 2 KiB of them; its count in bytes or records;
 *   - INT1: its threshold and data-ready interrupts (INT_SOURCE0), pulsed
 *     or latched, active high or low (INT_CONFIG), and INT_STATUS, which a
 *     read clears.  The pin is held on the header (the machine's "header"
 *     property), as a pin the outside holds.
 *
 * Not modelled: the self-test, the APEX motion features, the 20-bit FIFO
 * packets, FSYNC, the I2C and I3C sides, and noise: what the properties
 * say is what is measured.
 *
 * Its motion, read and set while it runs (qom-set):
 *   accel-x, accel-y, accel-z   in mg (default 0, 0, 1000: flat, at rest)
 *   gyro-x, gyro-y, gyro-z      in m°/s (default 0)
 *   temperature                 in m°C (default 25000)
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "qom/object.h"
#include "hw/core/qdev-properties.h"
#include "migration/vmstate.h"
#include "hw/core/boards.h"
#include "flipper.h"

#define TYPE_ICM42688P "icm42688p"
OBJECT_DECLARE_SIMPLE_TYPE(Icm42688pState, ICM42688P)

/* bank 0 */
#define R_DEVICE_CONFIG     0x11
#define R_INT_CONFIG        0x14
#define R_FIFO_CONFIG       0x16
#define R_TEMP_DATA1        0x1D
#define R_ACCEL_DATA_X1     0x1F
#define R_GYRO_DATA_X1      0x25
#define R_TMST_FSYNCH       0x2B
#define R_INT_STATUS        0x2D
#define R_FIFO_COUNTH       0x2E
#define R_FIFO_COUNTL       0x2F
#define R_FIFO_DATA         0x30
#define R_SIGNAL_PATH_RESET 0x4B
#define R_INTF_CONFIG0      0x4C
#define R_PWR_MGMT0         0x4E
#define R_GYRO_CONFIG0      0x4F
#define R_ACCEL_CONFIG0     0x50
#define R_FIFO_CONFIG1      0x5F
#define R_FIFO_CONFIG2      0x60
#define R_FIFO_CONFIG3      0x61
#define R_INT_CONFIG0       0x63
#define R_INT_CONFIG1       0x64
#define R_INT_SOURCE0       0x65
#define R_WHO_AM_I          0x75
#define R_REG_BANK_SEL      0x76
/* bank 4 */
#define R4_OFFSET_USER0     0x77

#define WHOAMI              0x47
#define FIFO_SIZE           2048
#define PACKET              16      /* header, accel 6, gyro 6, temp, tmst 2 */

/* INT_STATUS */
#define ST_DATA_RDY         (1 << 3)
#define ST_FIFO_THS         (1 << 2)
#define ST_FIFO_FULL        (1 << 1)
#define ST_RESET_DONE       (1 << 4)

struct Icm42688pState {
    SSIPeripheral parent_obj;

    uint8_t bank0[128];
    uint8_t bank4[128];
    uint8_t bank;
    /* the frame */
    bool first;                 /* the next byte is the address */
    bool read;
    uint8_t addr;
    uint8_t count_latch[2];     /* FIFO_COUNT as it was at COUNTH's read */
    /* the FIFO */
    uint8_t fifo[FIFO_SIZE];
    uint32_t head, used;
    /* INT1 */
    bool int1;                  /* asserted: the pin is held */
    uint8_t status;             /* INT_STATUS */
    uint16_t tmst;
    QEMUTimer *odr;
    QEMUTimer *pulse;
    /* the motion */
    int32_t accel[3], gyro[3], temp;
    char *int1_pin;
};

/* The output period of a sensor's ODR field, in ns; 0: none */
static int64_t odr_period(uint8_t field)
{
    static const uint32_t hz[16] = {
        [1] = 32000, [2] = 16000, [3] = 8000, [4] = 4000, [5] = 2000,
        [6] = 1000, [7] = 200, [8] = 100, [9] = 50, [10] = 25,
        [15] = 500,
    };
    return hz[field & 15] ? NANOSECONDS_PER_SECOND / hz[field & 15] : 0;
}

static bool gyro_on(Icm42688pState *s)
{
    return (s->bank0[R_PWR_MGMT0] & 0x0C) != 0;
}

static bool accel_on(Icm42688pState *s)
{
    return (s->bank0[R_PWR_MGMT0] & 0x03) >= 2;
}

static int64_t sample_period(Icm42688pState *s)
{
    if (gyro_on(s)) {
        return odr_period(s->bank0[R_GYRO_CONFIG0]);
    }
    if (accel_on(s)) {
        return odr_period(s->bank0[R_ACCEL_CONFIG0]);
    }
    return 0;
}

static int16_t clamp16(int64_t v)
{
    return v > INT16_MAX ? INT16_MAX : v < INT16_MIN ? INT16_MIN : v;
}

/* full scale: accel 16 g >> FS_SEL; gyro 2000 °/s >> FS_SEL */
static int16_t accel_raw(Icm42688pState *s, int axis)
{
    int fs = 16000 >> (s->bank0[R_ACCEL_CONFIG0] >> 5 & 3);   /* mg */

    if (!accel_on(s)) {
        return INT16_MIN;           /* the datasheet's "no data" value */
    }
    return clamp16((int64_t)s->accel[axis] * 32768 / fs);
}

/* the user offset of a gyro axis, in 1/32 °/s (12 bits, signed) */
static int gyro_offset(Icm42688pState *s, int axis)
{
    const uint8_t *o = &s->bank4[R4_OFFSET_USER0];
    int v;

    switch (axis) {
    case 0:
        v = o[0] | (o[1] & 0x0F) << 8;
        break;
    case 1:
        v = o[2] | (o[1] & 0xF0) << 4;
        break;
    default:
        v = o[3] | (o[4] & 0x0F) << 8;
        break;
    }
    return v & 0x800 ? v - 0x1000 : v;
}

static int16_t gyro_raw(Icm42688pState *s, int axis)
{
    int64_t fs = 2000000 >> (s->bank0[R_GYRO_CONFIG0] >> 5 & 7); /* m°/s */
    int64_t mdps = s->gyro[axis] + (int64_t)gyro_offset(s, axis) * 1000 / 32;

    if (!gyro_on(s)) {
        return INT16_MIN;
    }
    return clamp16(mdps * 32768 / fs);
}

/* TEMP_DATA: 132.48 LSB/°C from 25 °C; the FIFO's is 2.07 LSB/°C */
static int16_t temp_raw(Icm42688pState *s)
{
    return clamp16((int64_t)(s->temp - 25000) * 13248 / 100000);
}

static bool big_endian_data(Icm42688pState *s)
{
    return s->bank0[R_INTF_CONFIG0] & (1 << 4);
}

static bool big_endian_count(Icm42688pState *s)
{
    return s->bank0[R_INTF_CONFIG0] & (1 << 5);
}

static void put16(uint8_t *at, int16_t v, bool big)
{
    at[big ? 0 : 1] = (uint16_t)v >> 8;
    at[big ? 1 : 0] = v & 0xFF;
}

/* the data registers 0x1D..0x2A, as they read now */
static uint8_t data_reg(Icm42688pState *s, uint8_t addr)
{
    uint8_t regs[14];
    bool big = big_endian_data(s);

    put16(&regs[0], temp_raw(s), big);
    for (int i = 0; i < 3; i++) {
        put16(&regs[2 + 2 * i], accel_raw(s, i), big);
        put16(&regs[8 + 2 * i], gyro_raw(s, i), big);
    }
    return regs[addr - R_TEMP_DATA1];
}

static uint32_t fifo_count(Icm42688pState *s)
{
    /* INTF_CONFIG0 bit 6: in records */
    return s->bank0[R_INTF_CONFIG0] & (1 << 6) ? s->used / PACKET : s->used;
}

static uint32_t watermark(Icm42688pState *s)
{
    uint32_t w = s->bank0[R_FIFO_CONFIG2] | (s->bank0[R_FIFO_CONFIG3] & 0x0F)
                 << 8;
    return w ? w : 1;
}

/*
 * The pin, held on the header as the outside holds one: while the interrupt
 * is asserted, at its active level (INT_CONFIG bit 0: 1 high, 0 low); let
 * go of when it is not (the firmware pulls it to the idle level: Air Mouse
 * and the JS module pull it down and want a rising edge).
 */
static void drive_int1(Icm42688pState *s, bool active)
{
    g_autofree char *hold = NULL;
    Error *err = NULL;

    if (!s->int1_pin || !*s->int1_pin || active == s->int1) {
        return;
    }
    s->int1 = active;
    hold = g_strdup_printf("%s=%s", s->int1_pin,
                           !active ? "z" :
                           (s->bank0[R_INT_CONFIG] & 1) ? "1" : "0");
    if (!object_property_set_str(OBJECT(qdev_get_machine()), "header", hold,
                                 &err)) {
        error_report_err(err);
    }
}

static bool int1_latched(Icm42688pState *s)
{
    return s->bank0[R_INT_CONFIG] & (1 << 2);
}

/* an interrupt condition came: INT_STATUS has it, INT1 shows it if it is
 * routed there */
static void int_raise(Icm42688pState *s, uint8_t bits)
{
    uint8_t routed = 0;

    s->status |= bits;
    if ((bits & ST_FIFO_THS) && (s->bank0[R_INT_SOURCE0] & (1 << 2))) {
        routed |= ST_FIFO_THS;
    }
    if ((bits & ST_DATA_RDY) && (s->bank0[R_INT_SOURCE0] & (1 << 3))) {
        routed |= ST_DATA_RDY;
    }
    if ((bits & ST_FIFO_FULL) && (s->bank0[R_INT_SOURCE0] & (1 << 1))) {
        routed |= ST_FIFO_FULL;
    }
    if (!routed) {
        return;
    }
    drive_int1(s, true);
    if (!int1_latched(s)) {
        /* a pulse of 100 µs, or 8 µs (INT_CONFIG1 bit 6) */
        int64_t ns = s->bank0[R_INT_CONFIG1] & (1 << 6) ? 8000 : 100000;
        timer_mod(s->pulse, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ns);
    }
}

static void pulse_end(void *opaque)
{
    drive_int1(opaque, false);
}

static void fifo_push(Icm42688pState *s)
{
    uint8_t p[PACKET];
    bool big = big_endian_data(s);
    uint8_t cfg1 = s->bank0[R_FIFO_CONFIG1];

    if (!(cfg1 & 3)) {
        return;                     /* neither sensor goes to the FIFO */
    }
    /* header: accel (bit 6), gyro (bit 5), an ODR timestamp (bits 3:2 10) */
    p[0] = 0x40 | 0x20 | 0x08;
    for (int i = 0; i < 3; i++) {
        put16(&p[1 + 2 * i], (cfg1 & 1) ? accel_raw(s, i) : INT16_MIN, big);
        put16(&p[7 + 2 * i], (cfg1 & 2) ? gyro_raw(s, i) : INT16_MIN, big);
    }
    p[13] = clamp16((int64_t)(s->temp - 25000) * 207 / 100000);
    put16(&p[14], s->tmst, big);
    if (s->used + PACKET > FIFO_SIZE) {
        if (s->bank0[R_FIFO_CONFIG] & 0x80) {
            int_raise(s, ST_FIFO_FULL);     /* stop-on-full: the new one is lost */
            return;
        }
        s->head = (s->head + PACKET) % FIFO_SIZE;   /* stream: the oldest */
        s->used -= PACKET;
    }
    for (int i = 0; i < PACKET; i++) {
        s->fifo[(s->head + s->used + i) % FIFO_SIZE] = p[i];
    }
    s->used += PACKET;
    if (fifo_count(s) >= watermark(s)) {
        int_raise(s, ST_FIFO_THS);
    }
}

static void odr_schedule(Icm42688pState *s)
{
    int64_t period = sample_period(s);

    if (!period) {
        timer_del(s->odr);
        return;
    }
    if (!timer_pending(s->odr)) {
        timer_mod(s->odr, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + period);
    }
}

static void odr_tick(void *opaque)
{
    Icm42688pState *s = opaque;
    int64_t period = sample_period(s);

    if (!period) {
        return;
    }
    s->tmst += period / 1000;       /* the timestamp counts µs */
    if ((s->bank0[R_FIFO_CONFIG] >> 6) != 0) {
        fifo_push(s);
    }
    int_raise(s, ST_DATA_RDY);
    timer_mod(s->odr, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + period);
}

static void soft_reset(Icm42688pState *s)
{
    memset(s->bank0, 0, sizeof(s->bank0));
    memset(s->bank4, 0, sizeof(s->bank4));
    s->bank0[R_DEVICE_CONFIG] = 0x00;
    s->bank0[R_INT_CONFIG] = 0x00;
    s->bank0[R_INTF_CONFIG0] = 0x30;        /* big endian, as after reset */
    s->bank0[R_GYRO_CONFIG0] = 0x06;        /* 2000 °/s, 1 kHz */
    s->bank0[R_ACCEL_CONFIG0] = 0x06;       /* 16 g, 1 kHz */
    s->bank0[R_FIFO_CONFIG1] = 0x00;
    s->bank0[R_FIFO_CONFIG2] = 0x00;
    s->bank0[R_FIFO_CONFIG3] = 0x00;
    s->bank0[R_INT_CONFIG1] = 0x10;
    s->bank0[R_INT_SOURCE0] = 0x10;
    s->bank = 0;
    s->head = s->used = 0;
    s->status = ST_RESET_DONE;
    timer_del(s->odr);
    timer_del(s->pulse);
    drive_int1(s, false);
}

static uint8_t read_reg(Icm42688pState *s, uint8_t addr)
{
    if (addr == R_REG_BANK_SEL) {
        return s->bank;
    }
    if (s->bank == 4) {
        return s->bank4[addr];
    }
    if (s->bank != 0) {
        return 0;
    }
    switch (addr) {
    case R_WHO_AM_I:
        return WHOAMI;
    case R_INT_STATUS: {
        uint8_t v = s->status;

        s->status = 0;              /* cleared on read */
        if (int1_latched(s)) {
            drive_int1(s, false);
        }
        return v;
    }
    case R_FIFO_COUNTH:
    case R_FIFO_COUNTL: {
        uint16_t n = fifo_count(s);
        bool big = big_endian_count(s);

        if (addr == R_FIFO_COUNTH) {
            /* the pair is latched at the read of the first of them */
            s->count_latch[0] = big ? n >> 8 : n & 0xFF;
            s->count_latch[1] = big ? n & 0xFF : n >> 8;
            return s->count_latch[0];
        }
        return s->count_latch[1];
    }
    case R_FIFO_DATA: {
        uint8_t v;

        if (!s->used) {
            return 0xFF;            /* an empty FIFO reads so */
        }
        v = s->fifo[s->head];
        s->head = (s->head + 1) % FIFO_SIZE;
        s->used--;
        return v;
    }
    default:
        if (addr >= R_TEMP_DATA1 && addr < R_TMST_FSYNCH) {
            return data_reg(s, addr);
        }
        return s->bank0[addr];
    }
}

static void write_reg(Icm42688pState *s, uint8_t addr, uint8_t v)
{
    if (addr == R_REG_BANK_SEL) {
        s->bank = v & 7;
        return;
    }
    if (s->bank == 4) {
        s->bank4[addr] = v;
        return;
    }
    if (s->bank != 0) {
        return;
    }
    switch (addr) {
    case R_DEVICE_CONFIG:
        if (v & 1) {
            soft_reset(s);
            return;
        }
        break;
    case R_SIGNAL_PATH_RESET:
        if (v & (1 << 1)) {         /* FIFO_FLUSH */
            s->head = s->used = 0;
        }
        return;
    case R_WHO_AM_I:
    case R_INT_STATUS:
    case R_FIFO_COUNTH:
    case R_FIFO_COUNTL:
    case R_FIFO_DATA:
        return;
    case R_FIFO_CONFIG:
        if ((v >> 6) == 0) {        /* bypass: the FIFO is emptied */
            s->head = s->used = 0;
        }
        break;
    default:
        if (addr >= R_TEMP_DATA1 && addr < R_TMST_FSYNCH) {
            return;
        }
        break;
    }
    s->bank0[addr] = v;
    if (addr == R_PWR_MGMT0 || addr == R_GYRO_CONFIG0 ||
        addr == R_ACCEL_CONFIG0) {
        timer_del(s->odr);
        odr_schedule(s);
    }
}

static uint32_t icm42688p_transfer(SSIPeripheral *dev, uint32_t tx)
{
    Icm42688pState *s = ICM42688P(dev);
    uint8_t ret = 0;

    if (s->first) {
        s->first = false;
        s->read = tx & 0x80;
        s->addr = tx & 0x7F;
        return 0;
    }
    if (s->read) {
        ret = read_reg(s, s->addr);
    } else {
        write_reg(s, s->addr, tx);
    }
    if (s->addr != R_FIFO_DATA || s->bank != 0) {
        s->addr = (s->addr + 1) & 0x7F;
    }
    return ret;
}

static int icm42688p_set_cs(SSIPeripheral *dev, bool level)
{
    Icm42688pState *s = ICM42688P(dev);

    if (!level) {                   /* selected: a new frame */
        s->first = true;
    }
    return 0;
}

static void icm42688p_reset_hold(Object *obj, ResetType type)
{
    Icm42688pState *s = ICM42688P(obj);

    soft_reset(s);
    s->first = true;
}

/* ---- the motion, as properties */

static int32_t *motion(Icm42688pState *s, const char *name)
{
    if (g_str_has_prefix(name, "accel-")) {
        return &s->accel[name[6] - 'x'];
    }
    if (g_str_has_prefix(name, "gyro-")) {
        return &s->gyro[name[5] - 'x'];
    }
    return &s->temp;
}

static void motion_get(Object *obj, Visitor *v, const char *name,
                       void *opaque, Error **errp)
{
    int32_t value = *motion(ICM42688P(obj), name);

    visit_type_int32(v, name, &value, errp);
}

static void motion_set(Object *obj, Visitor *v, const char *name,
                       void *opaque, Error **errp)
{
    int32_t value;

    if (visit_type_int32(v, name, &value, errp)) {
        *motion(ICM42688P(obj), name) = value;
    }
}

static void icm42688p_realize(SSIPeripheral *d, Error **errp)
{
    Icm42688pState *s = ICM42688P(d);

    s->odr = timer_new_ns(QEMU_CLOCK_VIRTUAL, odr_tick, s);
    s->pulse = timer_new_ns(QEMU_CLOCK_VIRTUAL, pulse_end, s);
    soft_reset(s);
    s->first = true;
}

static void icm42688p_init(Object *obj)
{
    Icm42688pState *s = ICM42688P(obj);
    static const char *const names[] = {
        "accel-x", "accel-y", "accel-z", "gyro-x", "gyro-y", "gyro-z",
        "temperature",
    };

    s->accel[2] = 1000;             /* flat, at rest: 1 g up */
    s->temp = 25000;
    for (size_t i = 0; i < ARRAY_SIZE(names); i++) {
        object_property_add(obj, names[i], "int32", motion_get, motion_set,
                            NULL, NULL);
    }
}

static int icm42688p_post_load(void *opaque, int version)
{
    Icm42688pState *s = opaque;

    if (s->head >= FIFO_SIZE || s->used > FIFO_SIZE || s->bank > 7 ||
        s->addr > 0x7F) {
        return -EINVAL;
    }
    return 0;
}

static const VMStateDescription vmstate_icm42688p = {
    .name = TYPE_ICM42688P,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = icm42688p_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_SSI_PERIPHERAL(parent_obj, Icm42688pState),
        VMSTATE_UINT8_ARRAY(bank0, Icm42688pState, 128),
        VMSTATE_UINT8_ARRAY(bank4, Icm42688pState, 128),
        VMSTATE_UINT8(bank, Icm42688pState),
        VMSTATE_BOOL(first, Icm42688pState),
        VMSTATE_BOOL(read, Icm42688pState),
        VMSTATE_UINT8(addr, Icm42688pState),
        VMSTATE_UINT8_ARRAY(count_latch, Icm42688pState, 2),
        VMSTATE_UINT8_ARRAY(fifo, Icm42688pState, FIFO_SIZE),
        VMSTATE_UINT32(head, Icm42688pState),
        VMSTATE_UINT32(used, Icm42688pState),
        VMSTATE_BOOL(int1, Icm42688pState),
        VMSTATE_UINT8(status, Icm42688pState),
        VMSTATE_UINT16(tmst, Icm42688pState),
        VMSTATE_TIMER_PTR(odr, Icm42688pState),
        VMSTATE_TIMER_PTR(pulse, Icm42688pState),
        VMSTATE_INT32_ARRAY(accel, Icm42688pState, 3),
        VMSTATE_INT32_ARRAY(gyro, Icm42688pState, 3),
        VMSTATE_INT32(temp, Icm42688pState),
        VMSTATE_END_OF_LIST()
    }
};

static const Property icm42688p_props[] = {
    /* the pin of the Flipper's that INT1 is wired to ("": none) */
    DEFINE_PROP_STRING("int1-pin", Icm42688pState, int1_pin),
};

static void icm42688p_instance_post_init(Object *obj)
{
    Icm42688pState *s = ICM42688P(obj);

    if (!s->int1_pin) {
        s->int1_pin = g_strdup("PB2");
    }
}

static void icm42688p_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    DeviceClass *dc = DEVICE_CLASS(klass);

    device_class_set_props(dc, icm42688p_props);
    dc->vmsd = &vmstate_icm42688p;
    dc->desc = "ICM-42688-P six-axis IMU (Video Game Module)";
    k->realize = icm42688p_realize;
    k->transfer = icm42688p_transfer;
    k->set_cs = icm42688p_set_cs;
    k->cs_polarity = SSI_CS_LOW;
    RESETTABLE_CLASS(klass)->phases.hold = icm42688p_reset_hold;
}

static const TypeInfo icm42688p_types[] = {
    {
        .name = TYPE_ICM42688P,
        .parent = TYPE_SSI_PERIPHERAL,
        .instance_size = sizeof(Icm42688pState),
        .instance_init = icm42688p_init,
        .instance_post_init = icm42688p_instance_post_init,
        .class_init = icm42688p_class_init,
    },
};
DEFINE_TYPES(icm42688p_types)
