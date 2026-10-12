import os
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


def sandboxed_argv(
    argv: list[str], workspace: Path, project: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    if sys.platform != "darwin":
        raise ValueError("sandbox is unavailable")
    root = workspace.resolve()
    tmp = root / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env["HOME"] = str(root)
    env["TMPDIR"] = str(tmp)
    env[PROFILE_ENV] = seatbelt_profile(root, project)
    return [sys.executable, "-I", "-c", RUNNER, *argv], env
