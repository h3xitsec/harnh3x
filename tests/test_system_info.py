"""The system_info tool on Darwin and Linux fixtures."""

import json
from pathlib import Path

from harnh3x import skills, tools
from harnh3x.tools import execute_tool
from harnh3x.tools import system as host_system


class _HostProbe:
    def __init__(self, files=None, links=None, commands=None, sysctl=None):
        self.files = files or {}
        self.links = links or {}
        self.commands = commands or {}
        self.sysctl = sysctl or {}

    def read(self, path):
        return self.files.get(path)

    def list(self, path):
        prefix = path.rstrip("/") + "/"
        names = set()
        known = path in self.files or path in self.links
        for key in (*self.files, *self.links):
            if key.startswith(prefix):
                known = True
                rest = key[len(prefix) :]
                if rest:
                    names.add(rest.split("/")[0])
        if not known:
            return None
        return sorted(names)

    def exists(self, path):
        if path in self.files or path in self.links:
            return True
        prefix = path.rstrip("/") + "/"
        return any(key.startswith(prefix) for key in (*self.files, *self.links))

    def link(self, path):
        return self.links.get(path)

    def which(self, name):
        if name in self.commands:
            return name
        return None

    def run(self, argv, timeout):
        del timeout
        name = Path(argv[0]).name
        if name == "sysctl":
            key = argv[-1]
            if key not in self.sysctl:
                return None, "sysctl: unknown oid"
            return self.sysctl[key], None
        if name not in self.commands:
            return None, f"{name} is not available"
        result = self.commands[name]
        if isinstance(result, tuple):
            return result
        return result, None


_DARWIN_PROFILER = """
Hardware:

    Hardware Overview:

      Model Name: MacBook Pro
      Model Identifier: MacBookPro18,3
      Chip: Apple M4
      Total Number of Cores: 10 (4 performance and 6 efficiency)
      Memory: 24 GB

Memory:

    Memory:

      Size: 24 GB
      Type: LPDDR5
      Speed: 7500 MT/s

Storage:

    APPLE SSD AP0512Z:

      Medium Type: SSD
      Protocol: NVMe
      Capacity: 512 GB (549755813888 bytes)
      Mount Point: /

USB:

    USB 3.1 Bus:

      Host Controller Driver: AppleUSBXHCI

        USB Receiver:

          Product ID: 0xc52b
          Manufacturer: Logitech

Displays:

    Color LCD:

      Resolution: 3024 x 1964 Retina

PCI:

    Apple M4:

      Type: GPU

Power:

    Battery Information:

      Charge Information:

          State of Charge (%): 86

      Health Information:

          Cycle Count: 42

Network:

    Ethernet:

      Type: Ethernet
      BSD Device Name: en0

      Ethernet:

          MAC Address: aa:bb:cc:dd:ee:ff
"""


_DARWIN_SYSCTL = {
    "hw.model": "MacBookPro18,3",
    "hw.memsize": "25769803776",
    "hw.ncpu": "10",
    "hw.physicalcpu": "10",
    "hw.logicalcpu": "10",
    "kern.hostname": "mbp",
    "kern.boottime": "{ sec = 1700000000, usec = 0 }",
    "kern.osrelease": "24.1.0",
}


_DARWIN_SW = """ProductName:\tmacOS
ProductVersion:\t15.1
BuildVersion:\t24B83
"""


def _darwin_probe(profiler=_DARWIN_PROFILER):
    return _HostProbe(
        commands={"sw_vers": _DARWIN_SW, "system_profiler": profiler},
        sysctl=_DARWIN_SYSCTL,
    )


def _linux_files(chassis="3"):
    files = {
        "/etc/os-release": 'NAME="Ubuntu"\nVERSION="24.04.1 LTS (Noble Numbat)"\n',
        "/proc/sys/kernel/ostype": "Linux",
        "/proc/sys/kernel/osrelease": "6.8.0-45-generic",
        "/proc/sys/kernel/hostname": "devbox",
        "/proc/uptime": "90061.0 0",
        "/proc/meminfo": "MemTotal:       16777216 kB\n",
        "/proc/cpuinfo": "processor\t: 0\nmodel name\t: Intel placeholder\n",
        "/sys/class/dmi/id/sys_vendor": "Dell Inc.",
        "/sys/class/dmi/id/product_name": "XPS 15",
        "/sys/bus/usb/devices/1-1/manufacturer": "Logitech",
        "/sys/bus/usb/devices/1-1/product": "USB Receiver",
        "/sys/class/drm/card0-eDP-1/status": "connected",
        "/sys/class/power_supply/BAT0/type": "Battery",
        "/sys/class/power_supply/BAT0/capacity": "86",
        "/sys/class/power_supply/BAT0/status": "Discharging",
        "/sys/class/power_supply/BAT0/cycle_count": "42",
        "/sys/class/net/lo/address": "00:00:00:00:00:00",
        "/sys/class/net/docker0/address": "02:42:00:00:00:01",
        "/sys/class/net/enp0s31f6/address": "aa:bb:cc:dd:ee:ff",
        "/sys/class/net/enp0s31f6/operstate": "up",
    }
    if chassis is not None:
        files["/sys/class/dmi/id/chassis_type"] = chassis
    return files


_LINUX_LSBLK = json.dumps(
    {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "size": 512110190592,
                "type": "disk",
                "model": "Samsung SSD 980",
                "tran": "nvme",
                "rota": False,
                "mountpoint": None,
                "children": [
                    {
                        "name": "nvme0n1p2",
                        "type": "part",
                        "mountpoint": "/",
                    }
                ],
            }
        ]
    }
)


_LINUX_LSCPU = """Architecture: x86_64
CPU(s): 8
Model name: Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz
Core(s) per socket: 4
Socket(s): 1
"""


_LINUX_LSPCI = (
    '00:02.0 "VGA compatible controller" "Intel Corporation" "UHD Graphics"\n'
)


def _linux_probe(chassis="3"):
    return _HostProbe(
        files=_linux_files(chassis),
        links={"/sys/class/net/enp0s31f6/device/driver": "e1000e"},
        commands={
            "lsblk": _LINUX_LSBLK,
            "lscpu": _LINUX_LSCPU,
            "lspci": _LINUX_LSPCI,
            "dmidecode": (None, "dmidecode: Permission denied"),
        },
    )


def test_system_info_reads_a_mac(tmp_path):
    result = host_system.system_info(tmp_path, {}, probe=_darwin_probe())

    assert "name: macOS" in result
    assert "version: 15.1" in result
    assert "build: 24B83" in result
    assert "kernel: Darwin 24.1.0" in result
    assert "kind: laptop" in result
    assert "model: MacBook Pro" in result
    assert "identifier: MacBookPro18,3" in result
    assert "model: Apple M4" in result
    assert "architecture: arm64" in result
    assert "size: 24 GB" in result
    assert "speed: 7500 MT/s" in result
    assert "APPLE SSD AP0512Z" in result
    assert "NVMe" in result
    assert "USB: Logitech USB Receiver" in result
    assert "Display: Color LCD" in result
    assert "GPU: Apple M4" in result
    assert "Battery: 86%, 42 cycles" in result
    assert "en0" in result
    assert "aa:bb:cc:dd:ee:ff" in result
    assert "Notes" not in result


def test_system_info_reads_linux_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(host_system.sys, "platform", "linux")
    result = host_system.system_info(tmp_path, {}, probe=_linux_probe())

    assert "name: Ubuntu" in result
    assert "version: 24.04.1 LTS (Noble Numbat)" in result
    assert "kernel: Linux 6.8.0-45-generic" in result
    assert "kind: desktop" in result
    assert "vendor: Dell Inc." in result
    assert "model: XPS 15" in result
    assert "i7-8550U" in result
    assert "architecture: x86_64" in result
    assert "size: 16 GB" in result
    assert "Samsung SSD 980" in result
    assert "nvme" in result
    assert "mount /" in result
    assert "USB: Logitech USB Receiver" in result
    assert "Display: card0-eDP-1" in result
    assert "GPU: Intel Corporation UHD Graphics" in result
    assert "Battery: BAT0, 86%, Discharging, 42 cycles" in result
    assert "Network: enp0s31f6, e1000e, aa:bb:cc:dd:ee:ff, up" in result
    assert "docker0" not in result
    assert "dmidecode: Permission denied" in result


def test_system_info_maps_linux_chassis(tmp_path, monkeypatch):
    monkeypatch.setattr(host_system.sys, "platform", "linux")
    laptop = host_system.system_info(tmp_path, {}, probe=_linux_probe("8"))
    missing = host_system.system_info(tmp_path, {}, probe=_linux_probe(None))

    assert "kind: laptop" in laptop
    assert "kind: unknown" in missing
    assert not missing.startswith("Error:")


def test_system_info_keeps_sysctl_when_profiler_times_out(tmp_path):
    probe = _darwin_probe((None, "system_profiler timed out"))

    result = host_system.system_info(tmp_path, {}, probe=probe)

    assert "kind: laptop" in result
    assert "model: MacBookPro18,3" in result
    assert "size: 24 GB" in result
    assert "system_profiler timed out" in result
    assert not result.startswith("Error:")


def test_system_info_returns_one_section(tmp_path):
    result = host_system.system_info(
        tmp_path, {"sections": ["cpu"]}, probe=_darwin_probe()
    )

    assert result.startswith("CPU\n")
    assert "Apple M4" in result
    assert "Storage" not in result
    assert "APPLE SSD" not in result


def test_system_info_rejects_windows_and_unknown_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(host_system.sys, "platform", "win32")

    windows = execute_tool("system_info", {}, tmp_path)
    monkeypatch.setattr(host_system.sys, "platform", "darwin")
    unknown = execute_tool("system_info", {"sections": ["disk"]}, tmp_path)

    assert windows == "Error: system_info supports Linux and macOS only"
    assert unknown == "Error: unknown section 'disk'"


def test_system_info_is_a_hardware_diagnosis_tool():
    names = [tool["function"]["name"] for tool in tools.TOOLS]
    schema = next(
        tool for tool in tools.TOOLS if tool["function"]["name"] == "system_info"
    )
    skill = skills._CATALOG["hardware-diagnosis"]
    assigned = [tool["function"]["name"] for tool in skill.tools]

    assert "system_info" in names
    assert "command" not in schema["function"]["parameters"]["properties"]
    assert "system_info" in assigned
