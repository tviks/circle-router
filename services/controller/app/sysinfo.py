"""Состояние железа: процессор, память, диски, температуры.

Всё берётся из procfs и sysfs, без внешних утилит. `/proc` в контейнере — это
procfs хоста (он не разделяется namespace'ами), поэтому CPU и память видны
настоящие. А вот занятость файловых систем требует их увидеть: для этого корень
хоста примонтирован в /hostfs только на чтение.
"""

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("circle.sysinfo")

HOST_ROOT = Path(os.environ.get("CR_HOST_ROOT", "/hostfs"))
HWMON = Path("/sys/class/hwmon")

# Файловые системы, которые интересно показывать. Всё остальное — служебное.
REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat",
           "ntfs", "ntfs3", "zfs"}

# Служебные каталоги контейнерных рантаймов: там мелькают bind'ы тех же самых
# файловых систем, и в списке дисков они только шумят.
SKIP_MOUNT_PREFIXES = ("/var/lib/docker", "/var/lib/containerd", "/var/lib/kubelet",
                       "/var/lib/snapd", "/snap")

# hwmon именует себя по драйверу; по имени и понимаем, что это за железка.
CPU_SENSORS = {"coretemp", "k10temp", "zenpower", "cpu_thermal"}
DISK_SENSORS = {"nvme", "drivetemp"}


def _read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return default


def _read_int(path, default=None):
    value = _read(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── процессор ────────────────────────────────────────────────────────────────

def cpu_times():
    """{'cpu': (idle, total), 'cpu0': (...), ...} из /proc/stat."""
    result = {}
    try:
        content = Path("/proc/stat").read_text()
    except OSError:
        return result
    for line in content.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        name = parts[0]
        values = [int(v) for v in parts[1:]]
        if len(values) < 5:
            continue
        # idle + iowait считаем простоем: ждать диск — не работать.
        idle = values[3] + values[4]
        result[name] = (idle, sum(values))
    return result


def cpu_model():
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return model


def cpu_frequency_mhz():
    """Средняя текущая частота по ядрам, МГц."""
    values = []
    for path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
        khz = _read_int(path)
        if khz:
            values.append(khz / 1000)
    if values:
        return round(sum(values) / len(values))
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("cpu MHz"):
                values.append(float(line.split(":", 1)[1]))
    except (OSError, ValueError):
        pass
    return round(sum(values) / len(values)) if values else None


# ── память ───────────────────────────────────────────────────────────────────

def memory():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            number = rest.strip().split()
            if number:
                values[key] = int(number[0]) * 1024
    except (OSError, ValueError):
        return {}

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    used = max(total - available, 0)
    return {
        "total": total,
        "available": available,
        "used": used,
        "percent": round(used / total * 100, 1) if total else 0.0,
        "cached": values.get("Cached", 0) + values.get("Buffers", 0),
        "swap_total": swap_total,
        "swap_used": max(swap_total - swap_free, 0),
        "swap_percent": round((swap_total - swap_free) / swap_total * 100, 1)
        if swap_total else 0.0,
    }


# ── температуры ──────────────────────────────────────────────────────────────

def _hwmon_block_device(hwmon_dir):
    """Какому блочному устройству принадлежит датчик."""
    try:
        device = (hwmon_dir / "device").resolve()
    except OSError:
        return None
    block = device / "block"
    if block.is_dir():
        names = sorted(p.name for p in block.iterdir())
        if names:
            return names[0]
    # У NVMe блочное устройство лежит соседом: .../nvme/nvme0/nvme0n1
    for child in sorted(device.glob("nvme*n[0-9]*")):
        return child.name
    return None


def temperatures():
    """Плоский список датчиков с классификацией по железке."""
    sensors = []
    if not HWMON.is_dir():
        return sensors

    for hwmon_dir in sorted(HWMON.glob("hwmon*")):
        name = _read(hwmon_dir / "name", "")
        if name in CPU_SENSORS:
            kind = "cpu"
        elif name in DISK_SENSORS:
            kind = "disk"
        else:
            kind = "board"
        block = _hwmon_block_device(hwmon_dir) if kind == "disk" else None

        for input_path in sorted(hwmon_dir.glob("temp*_input")):
            raw = _read_int(input_path)
            if raw is None:
                continue
            prefix = input_path.name[:-len("_input")]
            label = _read(hwmon_dir / f"{prefix}_label") or name
            crit = _read_int(hwmon_dir / f"{prefix}_crit")
            high = _read_int(hwmon_dir / f"{prefix}_max")
            sensors.append({
                "source": name,
                "kind": kind,
                "label": label,
                "device": block,
                "value": round(raw / 1000, 1),
                "high": round(high / 1000, 1) if high else None,
                "critical": round(crit / 1000, 1) if crit else None,
            })
    return sensors


def cpu_temperature(sensors):
    """Одна цифра для карточки: пакет процессора, иначе максимум по ядрам."""
    cpu = [s for s in sensors if s["kind"] == "cpu"]
    if not cpu:
        board = [s for s in sensors if s["kind"] == "board"]
        return board[0]["value"] if board else None
    package = [s for s in cpu if "package" in s["label"].lower()]
    return (package or cpu)[0]["value"] if package else max(s["value"] for s in cpu)


# ── диски ────────────────────────────────────────────────────────────────────

def _host_mounts():
    """Файловые системы хоста.

    Читать `/hostfs/proc/mounts` бесполезно: это симлинк на `self/mounts`, и
    procfs всегда отдаёт таблицу того, кто читает, то есть контейнера. Зато
    рекурсивный bind корня хоста создаёт настоящие записи в нашей же таблице —
    с префиксом /hostfs. Их и разбираем.
    """
    prefix = str(HOST_ROOT).rstrip("/")
    content = _read("/proc/self/mounts", "")
    mounts, seen = [], set()

    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        source, target, fstype = parts[0], parts[1], parts[2]
        if fstype not in REAL_FS or not source.startswith("/dev/"):
            continue
        if target != prefix and not target.startswith(prefix + "/"):
            continue
        host_path = target[len(prefix):] or "/"
        if host_path.startswith(SKIP_MOUNT_PREFIXES):
            continue
        if host_path in seen:
            continue
        seen.add(host_path)
        mounts.append({"source": source, "mount": host_path,
                       "container_path": target, "fstype": fstype})
    return sorted(mounts, key=lambda m: m["mount"])


def _usage(container_path):
    """statvfs по пути внутри контейнера; при недоступности — None."""
    try:
        st = os.statvfs(container_path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used = total - free
    if not total:
        return None
    return {
        "total": total,
        "used": used,
        "available": avail,
        # Проценты считаем как df: место под root-резерв не в счёт.
        "percent": round(used / (used + avail) * 100, 1) if used + avail else 0.0,
    }


def diskstats():
    """{device: (read_sectors, write_sectors, io_ms)} из /proc/diskstats."""
    result = {}
    content = _read("/proc/diskstats", "")
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        # Только физические диски: у них есть каталог в /sys/block, а разделы
        # лежат внутри него. Заодно отсеиваются loop и device-mapper.
        if name.startswith(("loop", "dm-", "ram")):
            continue
        if not Path(f"/sys/block/{name}").is_dir():
            continue
        try:
            result[name] = (int(parts[5]), int(parts[9]), int(parts[12]))
        except ValueError:
            continue
    return result


def _base_disk(source):
    """Физический диск, на котором лежит файловая система.

    Для LVM и LUKS имя вида `ubuntu--vg-ubuntu--lv` ни с чем не сопоставляется,
    поэтому спускаемся к нижележащему устройству через /sys/block/<dm>/slaves.
    """
    name = Path(source).name
    if source.startswith("/dev/mapper/") or name.startswith("dm-"):
        try:
            name = (HOST_ROOT / source.lstrip("/")).resolve().name
        except OSError:
            pass
        slaves = Path(f"/sys/block/{name}/slaves")
        if slaves.is_dir():
            children = sorted(child.name for child in slaves.iterdir())
            if children:
                name = children[0]
    return re.sub(r"(p?\d+)$", "", name)


def disks(sensors=None):
    """Список дисков с занятостью и температурой."""
    sensors = sensors if sensors is not None else temperatures()
    by_device = {}
    for sensor in sensors:
        if sensor["kind"] == "disk" and sensor["device"]:
            current = by_device.get(sensor["device"])
            if current is None or sensor["value"] > current:
                by_device[sensor["device"]] = sensor["value"]

    result = []
    mounts = _host_mounts()
    for mount in mounts:
        usage = _usage(mount["container_path"])
        if not usage:
            continue
        device = Path(mount["source"]).name
        base = _base_disk(mount["source"])
        temperature = by_device.get(device) or by_device.get(base)
        result.append({k: v for k, v in mount.items() if k != "container_path"}
                      | {"device": device, **usage, "temperature": temperature})

    # Диски без файловой системы (или с чужой) всё равно показываем с температурой.
    mounted = {_base_disk(m["source"]) for m in mounts}
    for device, temperature in sorted(by_device.items()):
        if device not in mounted:
            result.append({"source": f"/dev/{device}", "mount": "", "fstype": "",
                           "device": device, "total": 0, "used": 0, "available": 0,
                           "percent": 0.0, "temperature": temperature})
    return result


def static_info():
    return {
        "cpu_model": cpu_model(),
        "cores": len(cpu_times()) - 1,
        "host_root_mounted": HOST_ROOT.is_dir(),
    }
