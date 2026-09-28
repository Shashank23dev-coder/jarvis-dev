"""Install an additional inherited seccomp filter before executing candidate code.

Docker's default profile stays enabled. Prevent same-UID code from suspending or
modifying the trusted PID-1 timeout process; exiting/killing PID 1 ends the sandbox.
"""
import ctypes
import errno
import os
import signal
import sys

lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
lib.seccomp_load.argtypes = [ctypes.c_void_p]
lib.seccomp_release.argtypes = [ctypes.c_void_p]

class Compare(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]

lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Compare)]
context = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW; Docker profile still applies.
if not context:
    raise RuntimeError('seccomp initialization failed')
try:
    for name, argument in [('kill', 1), ('tkill', 1), ('tgkill', 2),
                           ('rt_sigqueueinfo', 1), ('rt_tgsigqueueinfo', 2),
                           ('pidfd_send_signal', 1), ('ptrace', None),
                           ('process_vm_writev', None)]:
        number = lib.seccomp_syscall_resolve_name(name.encode())
        if number < 0:
            raise RuntimeError('Unsupported seccomp syscall')
        comparisons = []
        if argument is not None:
            comparisons.append(Compare(argument, 4, signal.SIGSTOP, 0))  # EQ
            if name != 'pidfd_send_signal':
                comparisons.append(Compare(0, 4, 1, 0))  # PID/TID 1 only
        # The child starts a new session; kill(0)/negative process groups cannot
        # reach PID 1. Linux kill(-1) also explicitly excludes namespace PID 1.
        rules = (Compare * len(comparisons))(*comparisons)
        if lib.seccomp_rule_add_array(context, 0x50000 | errno.EPERM, number,
                                      len(comparisons), rules):
            raise RuntimeError('seccomp rule failed')
    if lib.seccomp_load(context):
        raise RuntimeError('seccomp load failed')
finally:
    lib.seccomp_release(context)
os.execv(sys.argv[1], sys.argv[1:])
