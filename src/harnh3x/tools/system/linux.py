"""Linux report: /proc, /sys, and the optional lscpu, lsblk, dmidecode, lspci."""

import csv
import json
import re

from harnh3x.config import SHELL_TIMEOUT
from harnh3x.tools.system.format import (
    _clean,
    _command,
    _field,
    _note,
    _optional,
    _pairs,
    _rows,
    _size,
    _uptime,
)
from harnh3x.tools.system.probe import HostProbe

# SMBIOS chassis type codes.
_LAPTOP = {8, 9, 10, 14}
_DESKTOP = {3, 4, 6, 7}
_TABLET = {30, 31, 32}
_LSBLK = [
    "lsblk",
    "-J",
    "-b",
    "-o",
    "NAME,SIZE,TYPE,MODEL,TRAN,ROTA,MOUNTPOINT,FSTYPE",
]


def _os_release(text: str | None) -> dict[str, str]:
    found = {}
    if not text:
        return found
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found[key.strip()] = value.strip().strip('"')
    return found


def _linux(probe: HostProbe, notes: list[str]) -> dict[str, list[str]]:
    release = _os_release(probe.read("/etc/os-release"))
    ostype = _clean(probe.read("/proc/sys/kernel/ostype"))
    osrelease = _clean(probe.read("/proc/sys/kernel/osrelease"))
    hostname = _clean(probe.read("/proc/sys/kernel/hostname"))
    uptime = _linux_uptime(probe.read("/proc/uptime"))
    kernel = " ".join(bit for bit in (ostype, osrelease) if bit) or None
    vendor = _clean(probe.read("/sys/class/dmi/id/sys_vendor"))
    product = _clean(probe.read("/sys/class/dmi/id/product_name"))
    version = _clean(probe.read("/sys/class/dmi/id/product_version"))
    board = _clean(probe.read("/sys/class/dmi/id/board_name"))
    tree = _clean(probe.read("/proc/device-tree/model"))
    model = product or tree or board
    if version and model and version.lower() not in model.lower():
        model = f"{model} {version}"
    elif version and not model:
        model = version
    lscpu = _optional(probe, ["lscpu"], SHELL_TIMEOUT, notes)
    cpu = _linux_cpu(probe, lscpu, notes)
    memory, modules = _linux_memory(probe, notes)
    storage = _linux_storage(probe, notes)
    return {
        "os": _rows(
            _field("name", release.get("NAME") or release.get("PRETTY_NAME")),
            _field("version", release.get("VERSION")),
            _field("build", release.get("BUILD_ID")),
            _field("kernel", kernel),
            _field("hostname", hostname),
            _field("uptime", uptime),
        ),
        "machine": _rows(
            _field("kind", _chassis(probe.read("/sys/class/dmi/id/chassis_type"))),
            _field("vendor", vendor),
            _field("model", model),
        ),
        "cpu": cpu,
        "memory": _rows(_field("size", memory), _field("modules", modules)),
        "storage": storage,
        "hardware": _linux_hardware(probe, notes),
    }


def _chassis(raw: str | None) -> str:
    if not raw:
        return "unknown"
    token = raw.strip().split()[0]
    try:
        code = int(token)
    except ValueError:
        return "unknown"
    if code in _LAPTOP:
        return "laptop"
    if code in _DESKTOP:
        return "desktop"
    if code in _TABLET:
        return "tablet"
    return "unknown"


def _linux_uptime(raw: str | None) -> str | None:
    if not raw:
        return None
    token = raw.split()[0]
    try:
        return _uptime(int(float(token)))
    except ValueError:
        return None


def _linux_cpu(probe: HostProbe, lscpu: str | None, notes: list[str]) -> list[str]:
    fields = _pairs(lscpu or "")
    info = probe.read("/proc/cpuinfo") or ""
    model = fields.get("Model name") or _cpuinfo(info, "model name")
    if not model:
        model = _clean(probe.read("/proc/device-tree/model"))
    arch = fields.get("Architecture")
    if not arch:
        arch = _clean(_command(probe, ["uname", "-m"], SHELL_TIMEOUT, notes))
    logical = fields.get("CPU(s)") or _cpuinfo_count(info)
    physical = _physical_cores(fields) or _cpuinfo(info, "cpu cores")
    return _rows(
        _field("model", model),
        _field("architecture", arch),
        _field("physical", physical),
        _field("logical", logical),
    )


def _cpuinfo(text: str, key: str) -> str | None:
    prefix = key.lower()
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        if name.strip().lower() == prefix and value.strip():
            return value.strip()
    return None


def _cpuinfo_count(text: str) -> str | None:
    count = 0
    for line in text.splitlines():
        if line.lower().startswith("processor"):
            count += 1
    if count:
        return str(count)
    return None


def _physical_cores(fields: dict[str, str]) -> str | None:
    cores = fields.get("Core(s) per socket")
    sockets = fields.get("Socket(s)") or "1"
    if not cores:
        return None
    try:
        return str(int(cores) * int(sockets))
    except ValueError:
        return cores


def _linux_memory(probe: HostProbe, notes: list[str]) -> tuple[str | None, str | None]:
    meminfo = probe.read("/proc/meminfo") or ""
    size = None
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo, re.MULTILINE)
    if match:
        size = _size(int(match.group(1)) * 1024)
    text = _optional(probe, ["dmidecode", "-t", "memory"], SHELL_TIMEOUT, notes)
    modules = _dmi_modules(text or "")
    module_text = "; ".join(modules) if modules else None
    return size, module_text


def _dmi_modules(text: str) -> list[str]:
    modules = []
    size = None
    speed = None

    def flush() -> None:
        nonlocal size, speed
        if size and size.lower() not in {"no module installed", "not installed"}:
            modules.append(size if not speed else f"{size} {speed}")
        size = None
        speed = None

    for line in text.splitlines():
        if line.startswith("Memory Device"):
            flush()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "Size":
            size = value
        elif key == "Speed" and value and not value.lower().startswith("unknown"):
            speed = value
    flush()
    return modules


def _linux_storage(probe: HostProbe, notes: list[str]) -> list[str]:
    text = _optional(probe, _LSBLK, SHELL_TIMEOUT, notes)
    if text:
        parsed = _lsblk(text, notes)
        if parsed is not None:
            return parsed
    return _sysfs_storage(probe)


def _lsblk(text: str, notes: list[str]) -> list[str] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        _note(notes, "lsblk returned invalid JSON")
        return None
    devices = data.get("blockdevices") if isinstance(data, dict) else None
    if not isinstance(devices, list):
        _note(notes, "lsblk returned invalid JSON")
        return None
    lines = []
    for device in devices:
        if not isinstance(device, dict) or device.get("type") != "disk":
            continue
        lines.append("  - " + ", ".join(_disk_bits(device)))
    return lines


def _disk_bits(device: dict) -> list[str]:
    model = device.get("model")
    name = device.get("name") or "disk"
    label = model.strip() if isinstance(model, str) and model.strip() else str(name)
    bits = [label]
    size = device.get("size")
    if isinstance(size, int | str) and str(size).isdigit():
        bits.append(_size(int(size)))
    medium = _medium(device)
    if medium:
        bits.append(medium)
    mounts = _mounts(device)
    if mounts:
        bits.append("mount " + ", ".join(mounts))
    return bits


def _medium(device: dict) -> str:
    rota = device.get("rota")
    if rota == 1 or rota == "1":
        return "rotational"
    tran = device.get("tran")
    if isinstance(tran, str) and tran.strip():
        return tran.strip()
    return ""


def _mounts(device: dict) -> list[str]:
    found = []
    point = device.get("mountpoint")
    if isinstance(point, str) and point:
        found.append(point)
    children = device.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                found.extend(_mounts(child))
    return found


def _sysfs_storage(probe: HostProbe) -> list[str]:
    names = probe.list("/sys/block") or []
    mounts = _proc_mounts(probe.read("/proc/mounts"))
    lines = []
    for name in names:
        if name.startswith(("loop", "ram", "fd", "zram")):
            continue
        sectors = _clean(probe.read(f"/sys/block/{name}/size"))
        model = _clean(probe.read(f"/sys/block/{name}/device/model"))
        rota = _clean(probe.read(f"/sys/block/{name}/queue/rotational"))
        bits = [model or name]
        if sectors and sectors.isdigit():
            bits.append(_size(int(sectors) * 512))
        if rota == "1":
            bits.append("rotational")
        elif name.startswith("nvme"):
            bits.append("nvme")
        attached = [point for dev, point in mounts if dev.startswith(f"/dev/{name}")]
        if attached:
            bits.append("mount " + ", ".join(attached))
        lines.append("  - " + ", ".join(bits))
    return lines


def _proc_mounts(text: str | None) -> list[tuple[str, str]]:
    found = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("/dev/"):
            found.append((parts[0], parts[1]))
    return found


def _linux_hardware(probe: HostProbe, notes: list[str]) -> list[str]:
    lines: list[str] = []
    for name in probe.list("/sys/bus/usb/devices") or []:
        if ":" in name:
            continue
        product = _clean(probe.read(f"/sys/bus/usb/devices/{name}/product"))
        maker = _clean(probe.read(f"/sys/bus/usb/devices/{name}/manufacturer"))
        if not product and not maker:
            continue
        if maker and product and maker.lower() not in product.lower():
            label = f"{maker} {product}"
        else:
            label = product or maker or ""
        lines.append(f"  USB: {label}")
    for name in probe.list("/sys/class/drm") or []:
        if "-" not in name:
            continue
        status = _clean(probe.read(f"/sys/class/drm/{name}/status"))
        if status != "connected":
            continue
        lines.append(f"  Display: {name}")
    lspci = _optional(probe, ["lspci", "-mm"], SHELL_TIMEOUT, notes)
    lines.extend(_lspci(lspci or ""))
    for name in probe.list("/sys/class/power_supply") or []:
        kind = _clean(probe.read(f"/sys/class/power_supply/{name}/type"))
        if kind != "Battery":
            continue
        bits = [name]
        charge = _clean(probe.read(f"/sys/class/power_supply/{name}/capacity"))
        state = _clean(probe.read(f"/sys/class/power_supply/{name}/status"))
        cycles = _clean(probe.read(f"/sys/class/power_supply/{name}/cycle_count"))
        if charge:
            bits.append(f"{charge}%")
        if state:
            bits.append(state)
        if cycles:
            bits.append(f"{cycles} cycles")
        lines.append("  Battery: " + ", ".join(bits))
    for name in probe.list("/sys/class/net") or []:
        if name == "lo" or not probe.exists(f"/sys/class/net/{name}/device"):
            continue
        bits = [name]
        driver = probe.link(f"/sys/class/net/{name}/device/driver")
        mac = _clean(probe.read(f"/sys/class/net/{name}/address"))
        state = _clean(probe.read(f"/sys/class/net/{name}/operstate"))
        if driver:
            bits.append(driver)
        if mac:
            bits.append(mac)
        if state:
            bits.append(state)
        lines.append("  Network: " + ", ".join(bits))
    return lines


def _lspci(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            fields = next(csv.reader([line], delimiter=" ", skipinitialspace=True))
        except csv.Error:
            continue
        if len(fields) < 4:
            continue
        kind, vendor, device = fields[1], fields[2], fields[3]
        if "Network" in kind or "Ethernet" in kind:
            continue
        group = (
            "GPU" if any(word in kind for word in ("VGA", "3D", "Display")) else "PCI"
        )
        lines.append(f"  {group}: {vendor} {device}")
    return lines
