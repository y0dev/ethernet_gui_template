"""Shared pytest fixtures."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


def _find_compiler() -> str | None:
    for name in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return name
    return None


@pytest.fixture(scope="session")
def c_reference(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    """The reference C harness compiled with the host compiler.

    Skips (never fakes a pass) when no C compiler is available.
    """
    compiler = _find_compiler()
    if compiler is None:
        pytest.skip("no C compiler available; C reference comparisons not run")
    out_dir = tmp_path_factory.mktemp("c_reference")
    suffix = ".dll" if sys.platform == "win32" else ".so"
    library = out_dir / f"libreference{suffix}"
    source = FIXTURES / "c_reference" / "reference.c"
    command = [compiler, "-O0", "-shared", "-fPIC", "-fwrapv", "-o", str(library), str(source)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(f"C reference build failed:\n{result.stderr}")
    lib = ctypes.CDLL(str(library))

    i64, u8, u16, u32, i8, i16, i32 = (
        ctypes.c_int64, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32,
        ctypes.c_int8, ctypes.c_int16, ctypes.c_int32,
    )
    signatures = {
        "ref_to_u8": ([i64], u8), "ref_to_u16": ([i64], u16), "ref_to_u32": ([i64], u32),
        "ref_to_i8": ([i64], i8), "ref_to_i16": ([i64], i16), "ref_to_i32": ([i64], i32),
        "ref_div_i32": ([i32, i32], i32), "ref_mod_i32": ([i32, i32], i32),
        "ref_add_i32_wrap": ([i32, i32], i32), "ref_mul_i32_wrap": ([i32, i32], i32),
        "ref_add_u32": ([u32, u32], u32), "ref_sub_u32": ([u32, u32], u32),
        "ref_mul_u32": ([u32, u32], u32), "ref_add_u16": ([u16, u16], u16),
        "ref_shr_i32": ([i32, ctypes.c_int], i32), "ref_not_u32": ([u32], u32),
        "ref_crc32": ([ctypes.c_char_p, ctypes.c_size_t], u32),
        "ref_crc16_ccitt_false": ([ctypes.c_char_p, ctypes.c_size_t], u16),
        "ref_crc16_modbus": ([ctypes.c_char_p, ctypes.c_size_t], u16),
        "ref_crc8_smbus": ([ctypes.c_char_p, ctypes.c_size_t], u8),
        "ref_natural_size": ([], ctypes.c_size_t),
        "ref_natural_offset": ([ctypes.c_int], ctypes.c_size_t),
        "ref_packed_size": ([], ctypes.c_size_t),
        "ref_fill_natural": ([ctypes.c_char_p], None),
        "ref_fill_packed": ([ctypes.c_char_p], None),
        "ref_is_little_endian": ([], ctypes.c_int),
    }
    for name, (args, restype) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = restype
    return lib


@pytest.fixture
def db(tmp_path: Path):
    from app.core.database import Database, migrate

    database = Database(tmp_path / "test.db")
    migrate(database)
    yield database
    database.close()
