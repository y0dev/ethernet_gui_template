/*
 * Reference C harness for the embedded compatibility layer.
 *
 * These are NOT project sources.  They are small, independent, textbook
 * implementations compiled by the host C compiler so the Python helpers can be
 * checked against real C semantics (integer conversion, division, shifts,
 * bitwise CRC, struct layout) instead of against Python-only expectations.
 *
 * Build (done automatically by tests/conftest.py):
 *   cc -O0 -shared -fPIC -fwrapv -o libreference.so reference.c
 * -fwrapv makes signed overflow defined (wrapping) so it can be used as a
 * reference for the wrap_signed=True mode.
 */
#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

/* ---- integer conversions (C cast semantics) ---------------------------- */
EXPORT uint8_t  ref_to_u8(int64_t v)  { return (uint8_t)v; }
EXPORT uint16_t ref_to_u16(int64_t v) { return (uint16_t)v; }
EXPORT uint32_t ref_to_u32(int64_t v) { return (uint32_t)v; }
EXPORT int8_t   ref_to_i8(int64_t v)  { return (int8_t)v; }
EXPORT int16_t  ref_to_i16(int64_t v) { return (int16_t)v; }
EXPORT int32_t  ref_to_i32(int64_t v) { return (int32_t)v; }

/* ---- arithmetic -------------------------------------------------------- */
EXPORT int32_t  ref_div_i32(int32_t a, int32_t b) { return a / b; }
EXPORT int32_t  ref_mod_i32(int32_t a, int32_t b) { return a % b; }
EXPORT int32_t  ref_add_i32_wrap(int32_t a, int32_t b) { return a + b; } /* -fwrapv */
EXPORT int32_t  ref_mul_i32_wrap(int32_t a, int32_t b) { return a * b; } /* -fwrapv */
EXPORT uint32_t ref_add_u32(uint32_t a, uint32_t b) { return a + b; }
EXPORT uint32_t ref_sub_u32(uint32_t a, uint32_t b) { return a - b; }
EXPORT uint32_t ref_mul_u32(uint32_t a, uint32_t b) { return a * b; }
EXPORT uint16_t ref_add_u16(uint16_t a, uint16_t b) { return (uint16_t)(a + b); }
EXPORT int32_t  ref_shr_i32(int32_t a, int n) { return a >> n; }
EXPORT uint32_t ref_not_u32(uint32_t a) { return ~a; }

/* ---- bitwise (non table-driven) CRCs ----------------------------------- */
EXPORT uint32_t ref_crc32(const uint8_t *buf, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= buf[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 1u) ? (crc >> 1) ^ 0xEDB88320u : crc >> 1;
    }
    return crc ^ 0xFFFFFFFFu;
}

EXPORT uint16_t ref_crc16_ccitt_false(const uint8_t *buf, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)(buf[i] << 8);
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
    }
    return crc;
}

EXPORT uint16_t ref_crc16_modbus(const uint8_t *buf, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= buf[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 1u) ? (uint16_t)((crc >> 1) ^ 0xA001u) : (uint16_t)(crc >> 1);
    }
    return crc;
}

EXPORT uint8_t ref_crc8_smbus(const uint8_t *buf, size_t len)
{
    uint8_t crc = 0;
    for (size_t i = 0; i < len; i++) {
        crc ^= buf[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x80u) ? (uint8_t)((crc << 1) ^ 0x07u) : (uint8_t)(crc << 1);
    }
    return crc;
}

/* ---- struct layout ----------------------------------------------------- */
struct ref_natural {
    uint8_t  a;
    uint32_t b;
    uint16_t c;
    uint8_t  d[3];
    uint64_t e;
    int8_t   f;
};

#if defined(_MSC_VER)
#pragma pack(push, 1)
struct ref_packed { uint8_t a; uint32_t b; uint16_t c; uint8_t d[3]; uint64_t e; int8_t f; };
#pragma pack(pop)
#else
struct __attribute__((packed)) ref_packed {
    uint8_t a; uint32_t b; uint16_t c; uint8_t d[3]; uint64_t e; int8_t f;
};
#endif

EXPORT size_t ref_natural_size(void) { return sizeof(struct ref_natural); }
EXPORT size_t ref_natural_offset(int field)
{
    switch (field) {
    case 0: return offsetof(struct ref_natural, a);
    case 1: return offsetof(struct ref_natural, b);
    case 2: return offsetof(struct ref_natural, c);
    case 3: return offsetof(struct ref_natural, d);
    case 4: return offsetof(struct ref_natural, e);
    case 5: return offsetof(struct ref_natural, f);
    }
    return (size_t)-1;
}
EXPORT size_t ref_packed_size(void) { return sizeof(struct ref_packed); }

/* Serialize with the host's native layout and byte order. */
EXPORT void ref_fill_natural(uint8_t *out)
{
    struct ref_natural s;
    unsigned char *p = (unsigned char *)&s;
    for (size_t i = 0; i < sizeof s; i++) p[i] = 0; /* deterministic padding */
    s.a = 0x11; s.b = 0x22334455u; s.c = 0x6677; s.d[0] = 1; s.d[1] = 2; s.d[2] = 3;
    s.e = 0x0102030405060708ull; s.f = -2;
    for (size_t i = 0; i < sizeof s; i++) out[i] = p[i];
}

EXPORT void ref_fill_packed(uint8_t *out)
{
    struct ref_packed s;
    unsigned char *p = (unsigned char *)&s;
    s.a = 0x11; s.b = 0x22334455u; s.c = 0x6677; s.d[0] = 1; s.d[1] = 2; s.d[2] = 3;
    s.e = 0x0102030405060708ull; s.f = -2;
    for (size_t i = 0; i < sizeof s; i++) out[i] = p[i];
}

EXPORT int ref_is_little_endian(void) { const uint16_t x = 1; return *(const uint8_t *)&x; }
