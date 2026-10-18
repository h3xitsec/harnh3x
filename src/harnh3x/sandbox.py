import ctypes
import json
import os
import platform
import sys
from pathlib import Path

# Passed to the runner, then removed before exec so the command cannot read it.
PROFILE_ENV = "HARNESS_SANDBOX_PROFILE"

# Roots a process must read to start. User files under /Users are not included.
READ_ROOTS = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/opt",
    "/nix/store",
    "/run",
    "/private/var/db",
)

_DEVICES = (
    "/dev/null",
    "/dev/zero",
    "/dev/urandom",
    "/dev/random",
    "/dev/dtracehelper",
    "/dev/autofs_nowait",
)

_ENV_KEEP = ("PATH", "USER", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME")

# Landlock ABI 3 is Linux 6.2, the first version that mediates truncate.
_MIN_LANDLOCK_ABI = 3
_LINUX_MACHINES = frozenset({"x86_64", "aarch64"})
_LINUX_LIB_ROOTS = ("/lib", "/lib64")
_PROC_READS = ("/proc/self", "/proc/thread-self")
_LINUX_READ_WRITE_DEVICES = ("/dev/null",)
_LINUX_READ_DEVICES = ("/dev/zero", "/dev/urandom", "/dev/random")

# Applies the profile, then replaces this process. preexec_fn is unsafe once
# Textual has started threads, so the harness spawns this runner instead.
RUNNER = r"""
import ctypes
import os
import sys

profile = os.environ.get("HARNESS_SANDBOX_PROFILE")
if not profile:
    sys.stderr.write("sandbox profile is missing\n")
    raise SystemExit(127)
os.environ.pop("HARNESS_SANDBOX_PROFILE", None)

lib = ctypes.CDLL("/usr/lib/libsandbox.dylib")
lib.sandbox_init.argtypes = [
    ctypes.c_char_p,
    ctypes.c_uint64,
    ctypes.POINTER(ctypes.c_char_p),
]
lib.sandbox_init.restype = ctypes.c_int
err = ctypes.c_char_p()
rc = lib.sandbox_init(profile.encode(), 0, ctypes.byref(err))
if rc != 0:
    detail = "sandbox_init failed"
    if err.value:
        detail = err.value.decode("utf-8", "replace")
    sys.stderr.write(detail + "\n")
    raise SystemExit(127)

argv = sys.argv[1:]
if not argv:
    sys.stderr.write("command is required\n")
    raise SystemExit(127)
try:
    os.execvpe(argv[0], argv, os.environ)
except OSError as exc:
    sys.stderr.write(f"{exc}\n")
    raise SystemExit(127)
"""

# Same shape as RUNNER: a self-contained child, because python -I cannot
# import an editable checkout. It confines the filesystem with Landlock,
# then blocks network sockets and ptrace with seccomp, then execs.
LINUX_RUNNER = r"""
import ctypes
import json
import os
import struct
import sys

PROFILE = "HARNESS_SANDBOX_PROFILE"
MIN_ABI = 3
SYS_CREATE = 444
SYS_ADD = 445
SYS_RESTRICT = 446
CREATE_VERSION = 1
RULE_PATH = 1
O_PATH = 0x200000
O_CLOEXEC = 0x80000
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_KILL = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1
AF_UNIX = 1
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_IOCTL_DEV = 1 << 15
FS_RESOLVE_UNIX = 1 << 16
NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1
NET_BIND_UDP = 1 << 2
NET_CONNECT_UDP = 1 << 3
SCOPE_ABSTRACT = 1 << 0
SCOPE_SIGNAL = 1 << 1
LD = 0x20
JEQ = 0x15
RET = 0x06

# socket, ptrace, process_vm_readv, process_vm_writev, plus the audit arch.
ARCH = {
    "x86_64": {
        "audit": 0xC000003E,
        "socket": 41,
        "ptrace": 101,
        "process_vm_readv": 310,
        "process_vm_writev": 311,
    },
    "aarch64": {
        "audit": 0xC00000B7,
        "socket": 198,
        "ptrace": 117,
        "process_vm_readv": 270,
        "process_vm_writev": 271,
    },
}


def fail(msg):
    sys.stderr.write(msg + "\n")
    raise SystemExit(127)


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class SockFprog(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ushort),
        ("filter", ctypes.POINTER(SockFilter)),
    ]


libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.prctl.restype = ctypes.c_int
libc.prctl.argtypes = [
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
]


def die_errno(what):
    err = ctypes.get_errno()
    fail(f"{what} failed: {os.strerror(err)}")


def abi():
    libc.syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_uint,
    ]
    rc = libc.syscall(SYS_CREATE, None, 0, CREATE_VERSION)
    if rc < 0:
        die_errno("landlock")
    return int(rc)


def fs_mask(level):
    mask = (1 << 13) - 1
    if level >= 2:
        mask |= 1 << 13
    if level >= 3:
        mask |= 1 << 14
    if level >= 5:
        mask |= 1 << 15
    if level >= 9:
        mask |= FS_RESOLVE_UNIX
    return mask


def net_mask(level):
    if level < 4:
        return 0
    mask = NET_BIND_TCP | NET_CONNECT_TCP
    if level >= 10:
        mask |= NET_BIND_UDP | NET_CONNECT_UDP
    return mask


def ruleset_size(level):
    if level >= 6:
        return 24
    if level >= 4:
        return 16
    return 8


def create_ruleset(level, mask):
    size = ruleset_size(level)
    buf = bytearray(size)
    struct.pack_into("<Q", buf, 0, mask)
    if size >= 16:
        struct.pack_into("<Q", buf, 8, net_mask(level))
    if size >= 24:
        scope = SCOPE_ABSTRACT | SCOPE_SIGNAL
        struct.pack_into("<Q", buf, 16, scope)
    raw = (ctypes.c_char * size).from_buffer_copy(bytes(buf))
    libc.syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_uint,
    ]
    ptr = ctypes.cast(raw, ctypes.c_void_p)
    rc = libc.syscall(SYS_CREATE, ptr, size, 0)
    if rc < 0:
        die_errno("landlock_create_ruleset")
    return int(rc)


def add_rule(ruleset, fd, access):
    attr = PathBeneath(access, fd)
    libc.syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    ptr = ctypes.cast(ctypes.byref(attr), ctypes.c_void_p)
    rc = libc.syscall(SYS_ADD, ruleset, RULE_PATH, ptr, 0)
    err = ctypes.get_errno()
    os.close(fd)
    if rc != 0:
        fail(f"landlock_add_rule failed: {os.strerror(err)}")


def allow(ruleset, paths, access):
    for path in paths:
        try:
            fd = os.open(path, O_PATH | O_CLOEXEC)
        except OSError as exc:
            fail(str(exc))
        add_rule(ruleset, fd, access)


def apply_landlock(level, policy):
    mask = fs_mask(level)
    ruleset = create_ruleset(level, mask)
    read = (
        FS_EXECUTE | FS_READ_FILE | FS_READ_DIR | FS_RESOLVE_UNIX
    ) & mask
    dev_read = (FS_READ_FILE | FS_IOCTL_DEV) & mask
    dev_rw = (FS_READ_FILE | FS_WRITE_FILE | FS_IOCTL_DEV) & mask
    allow(ruleset, policy["read"], read)
    allow(ruleset, policy["write"], mask)
    allow(ruleset, policy["read_write_devices"], dev_rw)
    allow(ruleset, policy["read_devices"], dev_read)
    allow(ruleset, policy["proc"], read)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        die_errno("no_new_privs")
    libc.syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    if libc.syscall(SYS_RESTRICT, ruleset, 0) != 0:
        die_errno("landlock_restrict_self")
    os.close(ruleset)


def bpf(code, k, jt=0, jf=0):
    return SockFilter(code, jt, jf, k)


def install_seccomp(machine):
    spec = ARCH.get(machine)
    if spec is None:
        fail("sandbox is unavailable")
    deny = SECCOMP_RET_ERRNO | EPERM
    ins = [
        bpf(LD, 4),
        bpf(JEQ, spec["audit"], 1, 0),
        bpf(RET, SECCOMP_RET_KILL),
        bpf(LD, 0),
        bpf(JEQ, spec["socket"], 4, 0),
        bpf(JEQ, spec["ptrace"], 5, 0),
        bpf(JEQ, spec["process_vm_readv"], 4, 0),
        bpf(JEQ, spec["process_vm_writev"], 3, 0),
        bpf(RET, SECCOMP_RET_ALLOW),
        bpf(LD, 16),
        bpf(JEQ, AF_UNIX, 1, 0),
        bpf(RET, deny),
        bpf(RET, SECCOMP_RET_ALLOW),
    ]
    filters = (SockFilter * len(ins))(*ins)
    prog = SockFprog(len(ins), filters)
    rc = libc.prctl(
        PR_SET_SECCOMP,
        SECCOMP_MODE_FILTER,
        ctypes.addressof(prog),
        0,
        0,
    )
    if rc != 0:
        die_errno("seccomp")


def strings(policy, key):
    value = policy.get(key)
    if not isinstance(value, list):
        fail("sandbox profile is invalid")
    if any(not isinstance(item, str) for item in value):
        fail("sandbox profile is invalid")
    return value


raw = os.environ.get(PROFILE)
if not raw:
    fail("sandbox profile is missing")
os.environ.pop(PROFILE, None)
try:
    policy = json.loads(raw)
except json.JSONDecodeError:
    fail("sandbox profile is invalid")
if not isinstance(policy, dict):
    fail("sandbox profile is invalid")
machine = policy.get("machine")
if not isinstance(machine, str):
    fail("sandbox profile is invalid")
for key in (
    "read",
    "write",
    "read_write_devices",
    "read_devices",
    "proc",
):
    policy[key] = strings(policy, key)

level = abi()
if level < MIN_ABI:
    fail("sandbox is unavailable")
apply_landlock(level, policy)
install_seccomp(machine)

argv = sys.argv[1:]
if not argv:
    fail("command is required")
try:
    os.execvpe(argv[0], argv, os.environ)
except OSError as exc:
    fail(str(exc))
"""


def _subpath(path: Path) -> str:
    text = str(path)
    if any(char in text for char in '()"\\\n\x00'):
        raise ValueError("sandbox path is not allowed")
    if path == Path("/"):
        raise ValueError("sandbox path is not allowed")
    return f'(subpath "{text}")'


def _read_roots(workspace: Path, project: Path | None = None) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(raw: Path | str) -> None:
        path = Path(raw).resolve()
        if path in seen or not path.exists():
            return
        seen.add(path)
        found.append(path)

    for root in READ_ROOTS:
        add(root)
    add(sys.base_prefix)
    add(sys.prefix)
    add(workspace)
    if project is not None:
        add(project)
    return found


def seatbelt_profile(workspace: Path, project: Path | None = None) -> str:
    root = workspace.resolve()
    granted = None if project is None else project.resolve()
    write_roots = [root]
    if granted is not None and granted != root:
        write_roots.append(granted)
    # exec needs to read the root directory itself. literal "/" does not
    # grant access to anything inside it.
    reads = '(literal "/") ' + " ".join(
        _subpath(path) for path in _read_roots(root, granted)
    )
    writes = " ".join(_subpath(path) for path in write_roots)
    devices = " ".join(f'(literal "{device}")' for device in _DEVICES)
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow signal)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm)",
            "(allow system-socket)",
            '(allow file-ioctl (literal "/dev/null"))',
            f"(allow file-read-data {devices})",
            f"(allow file-read* file-map-executable {reads})",
            f"(allow file-write* {writes})",
            "(deny network*)",
        ]
    )


def _child_env(workspace: Path) -> dict[str, str]:
    root = workspace.resolve()
    tmp = root / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env["HOME"] = str(root)
    env["TMPDIR"] = str(tmp)
    return env


def _listed(raw: Path | str) -> str | None:
    path = Path(raw).resolve()
    if path == Path("/") or not path.exists():
        return None
    return str(path)


def _unique(paths: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        found.append(path)
    return found


def linux_policy(
    workspace: Path, project: Path | None, machine: str
) -> dict[str, object]:
    """Read and write paths for the Linux runner. Proc paths stay literal."""
    reads: list[str] = []
    for root in (*READ_ROOTS, *_LINUX_LIB_ROOTS, sys.base_prefix, sys.prefix):
        listed = _listed(root)
        if listed is not None:
            reads.append(listed)
    root = str(workspace.resolve())
    writes = [root]
    if project is not None:
        granted = str(project.resolve())
        if granted != root:
            writes.append(granted)
    read_write = [
        listed
        for device in _LINUX_READ_WRITE_DEVICES
        if (listed := _listed(device)) is not None
    ]
    read_only = [
        listed
        for device in _LINUX_READ_DEVICES
        if (listed := _listed(device)) is not None
    ]
    return {
        "machine": machine,
        "read": _unique(reads),
        "write": writes,
        "read_write_devices": read_write,
        "read_devices": read_only,
        "proc": list(_PROC_READS),
    }


def landlock_abi() -> int:
    """Probe Landlock without restricting this process. Linux only."""
    if sys.platform != "linux":
        raise ValueError("sandbox is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_uint,
    ]
    rc = syscall(ctypes.c_long(444), None, 0, 1)
    if rc < 0:
        raise ValueError("sandbox is unavailable")
    return int(rc)


def _linux_argv(
    argv: list[str], workspace: Path, project: Path | None
) -> tuple[list[str], dict[str, str]]:
    machine = platform.machine()
    if machine not in _LINUX_MACHINES or landlock_abi() < _MIN_LANDLOCK_ABI:
        raise ValueError("sandbox is unavailable")
    env = _child_env(workspace)
    env[PROFILE_ENV] = json.dumps(linux_policy(workspace, project, machine))
    return [sys.executable, "-I", "-c", LINUX_RUNNER, *argv], env


def sandboxed_argv(
    argv: list[str], workspace: Path, project: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    if sys.platform == "darwin":
        env = _child_env(workspace)
        env[PROFILE_ENV] = seatbelt_profile(workspace, project)
        return [sys.executable, "-I", "-c", RUNNER, *argv], env
    if sys.platform == "linux":
        return _linux_argv(argv, workspace, project)
    raise ValueError("sandbox is unavailable")
