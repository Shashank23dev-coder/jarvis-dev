"""Trusted, non-dumpable PID 1. Container lifetime is bounded independently of CLI."""
import ctypes
import math
import os
import subprocess
import sys

seconds = float(sys.argv[1])
if not math.isfinite(seconds) or seconds <= 0:
    raise ValueError('Invalid deadline')
# Same-UID candidates must not open /proc/1/mem or ptrace the timer, even on hosts
# without Yama. Set before starting any untrusted code; exec occurs only in child.
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(4, 0, 0, 0, 0):  # PR_SET_DUMPABLE = 0
    raise OSError(ctypes.get_errno(), 'Could not protect supervisor')
process = subprocess.Popen(sys.argv[2:], start_new_session=True)
try:
    status = process.wait(timeout=seconds)
except subprocess.TimeoutExpired:
    status = 124
finally:
    # Exiting PID 1 kills every process in its PID namespace, even descendants
    # that changed sessions, ignored signals or closed stdout/stderr.
    if process.poll() is None:
        process.kill()
os._exit(status if status >= 0 else 128 - status)
