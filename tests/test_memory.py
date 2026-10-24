"""Memory guard: the runtime import stack must stay lean (run budget < 300MB).

Contract:
  - `import trading_bot.main` must NOT import pandas (it used to cost ~59MB
    in RSS before validate_ohlcv moved to plain Python);
  - ccxt must stay lazy (its eager package init costs ~74MB) -- it only loads
    on first real exchange access;
  - the import stack's peak RSS must stay well under the 300MB run ceiling,
    leaving headroom for candle history, markets and SQLite at runtime.

Peak-RSS read uses the Win32 API, so the test skips on non-Windows.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Child: import the runtime stack, self-report peak RSS + loaded heavy modules,
# then hold alive briefly so the output is flushed before any teardown.
CHILD = r"""
import ctypes
import sys
import time
from ctypes import wintypes


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


import trading_bot.main  # noqa: E402

k32 = ctypes.windll.kernel32
k32.GetCurrentProcess.restype = wintypes.HANDLE
psapi = ctypes.windll.psapi.GetProcessMemoryInfo
psapi.restype = wintypes.BOOL
psapi.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]

m = PMC()
m.cb = ctypes.sizeof(PMC)
if not psapi(k32.GetCurrentProcess(), ctypes.byref(m), m.cb):
    print("PEAK unavailable", flush=True)
    sys.exit(2)
print("PEAK %d" % (m.PeakWorkingSetSize // (1024 * 1024)), flush=True)
print("MODS pandas=%s ccxt=%s" % ("pandas" in sys.modules, "ccxt" in sys.modules),
      flush=True)
time.sleep(15)
"""

PEAK_LIMIT_MB = 120  # import stack expected ~25-30MB; 4x headroom still < 300MB run budget


@pytest.mark.skipif(sys.platform != "win32", reason="peak-RSS read uses Win32 API")
def test_runtime_import_stack_stays_lean():
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        peak_line = proc.stdout.readline()
        mods_line = proc.stdout.readline()
        if not peak_line.startswith("PEAK"):
            err = proc.stderr.read()
            pytest.fail("child did not report peak RSS: %r / stderr: %s"
                        % (peak_line, err.strip()[:400]))
        assert "unavailable" not in peak_line, peak_line.strip()
        peak_mb = int(peak_line.split()[1])
    finally:
        proc.kill()
        proc.wait(timeout=15)

    assert mods_line.split() == ["MODS", "pandas=False", "ccxt=False"], mods_line
    assert peak_mb < PEAK_LIMIT_MB, (
        "runtime import stack peaked at %dMB (limit %dMB); a heavy import "
        "likely crept back into the trading_bot import path" % (peak_mb, PEAK_LIMIT_MB)
    )
