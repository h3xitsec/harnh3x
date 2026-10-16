"""macOS report: sw_vers, sysctl, system_profiler, networksetup."""

import re
import time

from harnh3x.config import SHELL_MAX_TIMEOUT, SHELL_TIMEOUT
from harnh3x.tools.system.format import (
    _capacity,
    _clean,
    _command,
    _deep,
    _descendants,
    _field,
    _Node,
    _note,
    _pairs,
    _parse_tree,
    _rows,
    _section,
    _size,
    _uptime,
)
from harnh3x.tools.system.probe import HostProbe

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
_BLANK_MAC = {"00:00:00:00:00:00", "02:00:00:00:00:00"}
_BOOT = re.compile(r"sec\s*=\s*(\d+)")


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
