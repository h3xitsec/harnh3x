import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harnh3x.config import SHELL_MAX_TIMEOUT, SHELL_TIMEOUT
from harnh3x.tools.paths import cap_result

SECTIONS = ("os", "machine", "cpu", "memory", "storage", "hardware")
_HINT = (
    "call system_info again with one sections value: "
    "os, machine, cpu, memory, storage, or hardware"
)
_PROFILER_TIMEOUT = min(SHELL_MAX_TIMEOUT, max(SHELL_TIMEOUT, 45))
_PROFILER = [
    "/usr/sbin/system_profiler",
    "-detailLevel",
    "mini",
    "SPHardwareDataType",
    "SPMemoryDataType",
    "SPStorageDataType",
    "SPUSBDataType",
    "SPDisplaysDataType",
    "SPPowerDataType",
    "SPPCIDataType",
    "SPNetworkDataType",
]
_SYSCTL = (
    "hw.model",
    "hw.memsize",
    "hw.ncpu",
    "hw.physicalcpu",
    "hw.logicalcpu",
    "machdep.cpu.brand_string",
    "kern.hostname",
    "kern.boottime",
    "kern.osrelease",
)
_QUIET_SYSCTL = {"machdep.cpu.brand_string"}
_LAPTOP = {8, 9, 10, 14}
_DESKTOP = {3, 4, 6, 7}
_TABLET = {30, 31, 32}
_BLANK_MAC = {"00:00:00:00:00:00", "02:00:00:00:00:00"}
_PLACEHOLDERS = {
    "",
    "none",
    "system product name",
    "to be filled by o.e.m.",
    "default string",
    "not specified",
    "not available",
}
_KV = re.compile(r"^(\s*)([^:\n]+):\s*(.*?)\s*$")
_BYTES = re.compile(r"\(([0-9,]+) bytes\)")
_BOOT = re.compile(r"sec\s*=\s*(\d+)")
_LSBLK = [
    "lsblk",
    "-J",
    "-b",
    "-o",
    "NAME,SIZE,TYPE,MODEL,TRAN,ROTA,MOUNTPOINT,FSTYPE",
]


class HostProbe:
    def read(self, path: str) -> str | None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        cleaned = text.replace("\x00", "").strip()
        return cleaned or None

    def list(self, path: str) -> list[str] | None:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def link(self, path: str) -> str | None:
        try:
            return Path(os.readlink(path)).name
        except OSError:
            return None

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str], timeout: int) -> tuple[str | None, str | None]:
        name = Path(argv[0]).name
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, f"{name} timed out"
        except FileNotFoundError:
            return None, f"{name} is not available"
        except OSError as exc:
            return None, f"{name}: {exc.strerror or exc}"
        stdout = completed.stdout or ""
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            reason = detail[0] if detail else f"exit {completed.returncode}"
            if len(reason) > 200:
                reason = reason[:200]
            note = f"{name}: {reason}"
            if stdout.strip():
                return stdout, note
            return None, note
        return stdout, None


class _Node:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fields: dict[str, str] = {}
        self.children: list[_Node] = []


def system_info(workspace: Path, args: dict, probe: HostProbe | None = None) -> str:
    del workspace
    if sys.platform not in {"darwin", "linux"}:
        return "Error: system_info supports Linux and macOS only"
    chosen = _choose(args.get("sections"))
    if isinstance(chosen, str):
        return chosen
    host = probe if probe is not None else HostProbe()
    notes: list[str] = []
    if sys.platform == "darwin":
        parts = _darwin(host, notes)
    else:
        parts = _linux(host, notes)
    text = _render(parts, chosen, notes)
    return cap_result(text, hint=_HINT)


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": (
                "Read OS, machine, CPU, memory, storage, and attached hardware "
                "for this Linux or macOS host"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(SECTIONS)},
                        "description": (
                            "Sections to return. Omit for all of: "
                            + ", ".join(SECTIONS)
                        ),
                    }
                },
            },
        },
    }
]

FNS = {"system_info": system_info}


def _choose(raw: object) -> tuple[str, ...] | str:
    if raw is None:
        return SECTIONS
    if isinstance(raw, str) or not isinstance(raw, list):
        return "Error: sections must be a list"
    if not raw:
        return "Error: sections must be a non-empty list"
    chosen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return "Error: sections must be a list of strings"
        if item not in SECTIONS:
            return f"Error: unknown section '{item}'"
        if item not in chosen:
            chosen.append(item)
    return tuple(chosen)


def _render(
    parts: dict[str, list[str]], sections: tuple[str, ...], notes: list[str]
) -> str:
    titles = {
        "os": "OS",
        "machine": "Machine",
        "cpu": "CPU",
        "memory": "Memory",
        "storage": "Storage",
        "hardware": "Hardware",
    }
    blocks = []
    for name in sections:
        rows = parts.get(name) or ["  unavailable"]
        blocks.append(titles[name] + "\n" + "\n".join(rows))
    if notes:
        listed = "\n".join(f"  - {note}" for note in notes)
        blocks.append(f"Notes\n{listed}")
    return "\n\n".join(blocks)


def _note(notes: list[str], text: str | None) -> None:
    if text and text not in notes:
        notes.append(text)


def _command(
    probe: HostProbe,
    argv: list[str],
    timeout: int,
    notes: list[str],
    *,
    quiet: bool = False,
) -> str | None:
    text, note = probe.run(argv, timeout)
    if not quiet:
        _note(notes, note)
    return text


def _optional(
    probe: HostProbe, argv: list[str], timeout: int, notes: list[str]
) -> str | None:
    if probe.which(Path(argv[0]).name) is None:
        return None
    return _command(probe, argv, timeout, notes)


def _field(label: str, value: str | None) -> str | None:
    if not value:
        return None
    return f"  {label}: {value}"


def _rows(*fields: str | None) -> list[str]:
    return [field for field in fields if field]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.replace("\x00", "").strip()
    if text.lower() in _PLACEHOLDERS:
        return None
    return text


def _size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B" or abs(value - round(value)) < 0.05:
                return f"{round(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def _uptime(seconds: int) -> str:
    days, rem = divmod(max(seconds, 0), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _capacity(text: str) -> str:
    match = _BYTES.search(text)
    if match:
        return _size(int(match.group(1).replace(",", "")))
    return text


def _parse_tree(text: str) -> list[_Node]:
    roots: list[_Node] = []
    stack: list[tuple[int, _Node]] = []
    for line in text.splitlines():
        match = _KV.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        key = match.group(2).strip()
        value = match.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not value:
            node = _Node(key)
            if stack:
                stack[-1][1].children.append(node)
            else:
                roots.append(node)
            stack.append((indent, node))
        elif stack:
            stack[-1][1].fields[key] = value
    return roots


def _section(roots: list[_Node], name: str) -> _Node | None:
    for node in roots:
        if node.name == name:
            return node
    return None


def _descendants(node: _Node) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        found.append(child)
        found.extend(_descendants(child))
    return found


def _deep(node: _Node, key: str) -> str | None:
    if key in node.fields:
        return node.fields[key]
    for child in node.children:
        found = _deep(child, key)
        if found:
            return found
    return None


def _pairs(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        found[key.strip()] = value.strip()
    return found


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


def _darwin(probe: HostProbe, notes: list[str]) -> dict[str, list[str]]:
    sw = _pairs(_command(probe, ["/usr/bin/sw_vers"], SHELL_TIMEOUT, notes) or "")
    sysctl = {
        key: _clean(
            _command(
                probe,
                ["/usr/sbin/sysctl", "-n", key],
                SHELL_TIMEOUT,
                notes,
                quiet=key in _QUIET_SYSCTL,
            )
        )
        for key in _SYSCTL
    }
    profiler = _command(probe, _PROFILER, _PROFILER_TIMEOUT, notes) or ""
    tree = _parse_tree(profiler)
    hardware = _section(tree, "Hardware")
    overview = None
    if hardware is not None:
        for child in hardware.children:
            if child.name == "Hardware Overview":
                overview = child
                break
        if overview is None and hardware.fields:
            overview = hardware
    fields = overview.fields if overview is not None else {}
    model = fields.get("Model Name") or sysctl.get("hw.model")
    identifier = fields.get("Model Identifier") or sysctl.get("hw.model")
    chip = fields.get("Chip") or fields.get("Processor Name")
    brand = sysctl.get("machdep.cpu.brand_string")
    cpu_model = chip or brand
    if chip and chip.startswith("Apple"):
        arch = "arm64"
    elif brand and ("Intel" in brand or "AMD" in brand):
        arch = "x86_64"
    else:
        arch = None
    cores = fields.get("Total Number of Cores")
    memory_size = _bytes_field(sysctl.get("hw.memsize"))
    if memory_size is None:
        memory_size = fields.get("Memory")
    speed, mem_type, modules = _darwin_memory(_section(tree, "Memory"))
    boot = sysctl.get("kern.boottime")
    uptime = None
    if boot:
        match = _BOOT.search(boot)
        if match:
            uptime = _uptime(int(time.time()) - int(match.group(1)))
    kernel = sysctl.get("kern.osrelease")
    return {
        "os": _rows(
            _field("name", sw.get("ProductName")),
            _field("version", sw.get("ProductVersion")),
            _field("build", sw.get("BuildVersion")),
            _field("kernel", f"Darwin {kernel}" if kernel else None),
            _field("hostname", sysctl.get("kern.hostname")),
            _field("uptime", uptime),
        ),
        "machine": _rows(
            _field("kind", _mac_kind(model, identifier)),
            _field("vendor", "Apple"),
            _field("model", model),
            _field("identifier", identifier if identifier != model else None),
        ),
        "cpu": _rows(
            _field("model", cpu_model),
            _field("architecture", arch),
            _field("cores", cores),
            _field("physical", sysctl.get("hw.physicalcpu") or sysctl.get("hw.ncpu")),
            _field("logical", sysctl.get("hw.logicalcpu")),
        ),
        "memory": _rows(
            _field("size", memory_size),
            _field("speed", speed),
            _field("type", mem_type),
            _field("modules", modules),
        ),
        "storage": _darwin_storage(_section(tree, "Storage")),
        "hardware": _darwin_hardware(probe, tree, notes),
    }


def _bytes_field(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return _size(int(raw))
    except ValueError:
        return raw


def _mac_kind(model: str | None, identifier: str | None) -> str:
    ident = identifier or ""
    named = model or ""
    if ident.startswith("MacBook") or "MacBook" in named:
        return "laptop"
    desktop = ("iMac", "Macmini", "MacPro", "MacStudio")
    if ident.startswith(desktop):
        return "desktop"
    for label in ("iMac", "Mac mini", "Mac Pro", "Mac Studio"):
        if label in named:
            return "desktop"
    return "unknown"


def _darwin_memory(node: _Node | None) -> tuple[str | None, str | None, str | None]:
    if node is None:
        return None, None, None
    speeds = []
    types = []
    modules = []
    for item in (node, *_descendants(node)):
        speed = item.fields.get("Speed")
        kind = item.fields.get("Type")
        if speed and speed not in speeds:
            speeds.append(speed)
        if kind and kind not in types:
            types.append(kind)
        if item is node:
            continue
        size = item.fields.get("Size")
        if size and size.lower() not in {"empty", "no module installed"}:
            modules.append(size if not speed else f"{size} {speed}")
    module_text = "; ".join(modules) if len(modules) > 1 else None
    return (
        "; ".join(speeds) or None,
        "; ".join(types) or None,
        module_text,
    )


def _darwin_storage(node: _Node | None) -> list[str]:
    if node is None:
        return []
    grouped: list[dict[str, object]] = []
    index: dict[str, dict[str, object]] = {}
    for item in node.children:
        capacity = item.fields.get("Capacity") or item.fields.get("Size")
        drive = next(
            (child for child in item.children if child.name == "Physical Drive"),
            None,
        )
        name = item.name
        medium = item.fields.get("Protocol") or item.fields.get("Medium Type")
        if drive is not None:
            medium = drive.fields.get("Protocol") or drive.fields.get("Medium Type")
            device = drive.fields.get("Device Name")
            if medium == "Disk Image" or device == "Disk Image":
                continue
            name = device or name
        if not capacity and not medium:
            continue
        disk = index.get(name)
        if disk is None:
            disk = {
                "name": name,
                "size": _capacity(capacity) if capacity else None,
                "medium": medium,
                "mounts": [],
            }
            index[name] = disk
            grouped.append(disk)
        mount = item.fields.get("Mount Point")
        mounts = disk["mounts"]
        if mount and isinstance(mounts, list) and mount not in mounts:
            mounts.append(mount)
    lines = []
    for disk in grouped:
        bits = [str(disk["name"])]
        if disk["size"]:
            bits.append(str(disk["size"]))
        if disk["medium"]:
            bits.append(str(disk["medium"]))
        mounts = disk["mounts"]
        if isinstance(mounts, list) and mounts:
            bits.append("mount " + ", ".join(mounts))
        lines.append("  - " + ", ".join(bits))
    return lines


def _darwin_hardware(
    probe: HostProbe, roots: list[_Node], notes: list[str]
) -> list[str]:
    lines: list[str] = []
    usb = _section(roots, "USB")
    if usb is not None:
        for item in _descendants(usb):
            if "Product ID" not in item.fields and "Manufacturer" not in item.fields:
                continue
            if item.name.endswith("Bus"):
                continue
            maker = item.fields.get("Manufacturer", "")
            label = item.name
            if maker and maker.lower() not in label.lower():
                label = f"{maker} {label}"
            lines.append(f"  USB: {label}")
    lines.extend(_graphics(_section(roots, "Graphics/Displays")))
    displays = _section(roots, "Displays")
    if displays is not None:
        for item in _descendants(displays):
            resolution = item.fields.get("Resolution")
            if resolution:
                lines.append(f"  Display: {item.name}, {resolution}")
            elif item.name and item.fields:
                lines.append(f"  Display: {item.name}")
    pci = _section(roots, "PCI")
    if pci is not None:
        for item in _descendants(pci):
            kind = item.fields.get("Type") or item.fields.get("Driver")
            if not kind and not item.fields:
                continue
            label = item.name if not kind else f"{item.name}, {kind}"
            group = "GPU" if kind and "GPU" in kind.upper() else "PCI"
            lines.append(f"  {group}: {label}")
    power = _section(roots, "Power")
    if power is not None:
        charge = _deep(power, "State of Charge (%)")
        cycles = _deep(power, "Cycle Count")
        bits = []
        if charge:
            bits.append(f"{charge}%")
        if cycles:
            bits.append(f"{cycles} cycles")
        if bits:
            lines.append("  Battery: " + ", ".join(bits))
    ports, note = probe.run(
        ["/usr/sbin/networksetup", "-listallhardwareports"], SHELL_TIMEOUT
    )
    if note and "not available" not in note:
        _note(notes, note)
    parsed = _hardware_ports(ports or "")
    if parsed:
        lines.extend(parsed)
    else:
        network = _section(roots, "Network")
        if network is not None:
            lines.extend(_darwin_network(network))
    return lines


def _graphics(node: _Node | None) -> list[str]:
    if node is None:
        return []
    lines = []
    for item in node.children:
        kind = item.fields.get("Type")
        chipset = item.fields.get("Chipset Model") or item.name
        if kind:
            group = "GPU" if "GPU" in kind.upper() else "PCI"
            lines.append(f"  {group}: {chipset}, {kind}")
        for display in _descendants(item):
            resolution = display.fields.get("Resolution")
            if resolution:
                lines.append(f"  Display: {display.name}, {resolution}")
    return lines


def _hardware_ports(text: str) -> list[str]:
    lines = []
    port = None
    device = None
    mac = None

    def flush() -> None:
        nonlocal port, device, mac
        if port and device and mac and mac.lower() not in _BLANK_MAC:
            bits = [port]
            if device not in port:
                bits.append(device)
            bits.append(mac)
            lines.append("  Network: " + ", ".join(bits))
        port = None
        device = None
        mac = None

    for line in text.splitlines():
        if line.startswith("VLAN"):
            break
        if not line.strip():
            flush()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "Hardware Port":
            if port or device or mac:
                flush()
            port = value
        elif key == "Device":
            device = value
        elif key == "Ethernet Address":
            mac = value
    flush()
    return lines


def _darwin_network(node: _Node) -> list[str]:
    lines = []
    for child in node.children:
        mac = _deep(child, "MAC Address")
        device = _deep(child, "BSD Device Name")
        kind = child.fields.get("Type") or child.fields.get("Hardware")
        if not mac and not device:
            continue
        bits = [child.name]
        if kind and kind not in child.name:
            bits.append(kind)
        if device:
            bits.append(device)
        if mac:
            bits.append(mac)
        lines.append("  Network: " + ", ".join(bits))
    return lines


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
