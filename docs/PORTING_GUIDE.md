# C-to-Python Porting Guide

The original C implementation is the source of truth. A module is finished
only when a recorded comparison against that C code passes.

## Per-module procedure

1. **Import** the C tree in the Porting Workspace (Import C sources…). Every
   `.c` file becomes a module in *Not Analyzed* with its includes, external
   calls and top-level symbols listed.
2. **Analyze** — fill in the specification below (copy it to
   `docs/porting/<module>.md`). Confirm the scanner inventory against the real
   build (`gcc -E` with the project's flags). Set status *Analyzed*.
3. **Plan** — map every public symbol to its Python replacement in the
   Function mapping tab. Record known risks as *Compatibility Issue* notes.
   Set *Ready to Port* once dependencies are ported or have an abstraction.
4. **Port** (*In Progress*) — follow the rules below.
5. **Ported** — set the Python module path; status becomes available.
6. **Verify** (*Verification Required*) — run the comparison tests, then
   *Record verification run* with the real numbers.
7. **Verified** — only accepted by the application when the latest run
   compared against C, had passes and zero failures, and binary compatibility
   was not reported as different.

## Module specification template

```
Module:            <c path>  →  <python path>
Purpose:
Public interface:  <prototype> → <python signature>   (one line each)
Data types:        struct/union/enum/typedef → dataclass / IntEnum / StructLayout
Internal state:    globals and statics, and who owns them
Dependencies:      headers, other modules, HAL/OS calls
Error handling:    return codes / errno / asserts → Python behaviour
Side effects:      I/O, register writes, timing
Threading:         ISR context? locks? reentrancy?
Hardware/OS:       what goes behind app/core/compatibility/hal.py
Binary contract:   sizes, alignment, packing, endianness, CRC parameters
Python strategy:
Compatibility risks:
Intentional differences (need approval):
```

## Porting rules (from the project brief)

* Keep C names where it helps traceability (`crc16_update` stays
  `crc16_update`).
* Keep module boundaries; separate hardware access and transport from logic.
* Use `fixed_int` for every arithmetic expression whose C result depends on
  width or signedness. Decide explicitly whether signed overflow wraps
  (`wrap_signed=True`, i.e. the C build used `-fwrapv`) — never assume.
* Use `StructLayout` for anything that crosses a wire or a file, built from
  the C definition. Check `size` and `offsets` against `sizeof`/`offsetof`
  from the real target compiler.
* Bit fields: model the containing integer and use masks from the C source.
  Their layout is implementation-defined; do not guess.
* CRCs: take the parameters from the C code (poly, init, reflection, xorout)
  and verify; do not pick a catalogue entry because its name matches.
* Do not drop error handling. If a C function returns `-1`, either keep the
  return code or raise — and record which as a *Behavioral Difference* note.
* Do not replace algorithms with approximations.
* Use `ctypes` only when real ABI compatibility is needed (calling the C
  library itself), not for ordinary ports.

## Verification

Preferred: compile the original C into a shared library and call it through
`ctypes` with identical inputs (`tests/conftest.py::c_reference` shows the
pattern: build with the host compiler, declare `argtypes`/`restype`, skip if
no compiler — never pass).

When the C cannot be built or run on the host, use **golden vectors**:
inputs and outputs captured from the original C (on target or in a
simulator), stored under `tests/fixtures/golden/<module>/`, with a note of how
they were produced.

Each verification suite should cover: byte-for-byte buffer comparison,
boundaries (min/max, zero-length, full buffers), invalid input, endianness and
signedness, and every documented error path.

Record in the workspace: reference kind, functions tested, pass/fail counts,
binary compatibility result, behavioral differences, remaining risks, and a
path to the test log.
