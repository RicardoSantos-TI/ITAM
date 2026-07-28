#!/usr/bin/env python3
"""
Agente de Inventario de TI
--------------------------
Coleta hardware, sistema operacional, software instalado, rede,
dispositivos conectados e metricas de saude, e envia para o servidor.

Multiplataforma: usa WMI/registro no Windows e comandos nativos como
fallback em Linux/macOS. Toda coleta e isolada em try/except para que
uma falha parcial nunca derrube o envio.

Uso:
    python agent.py            # envia uma vez e sai (--once e o padrao)
    python agent.py --loop     # roda em laco no intervalo do config
    python agent.py --dry-run  # coleta e imprime o JSON, nao envia
"""

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
import uuid

# ---- Dependencias externas (pip install -r requirements.txt) --------------
try:
    import psutil
except ImportError:
    psutil = None

try:
    import requests
except ImportError:
    requests = None

# ---- Dependencias so-Windows ----------------------------------------------
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"

wmi = None
winreg = None
if IS_WINDOWS:
    try:
        import wmi as _wmi
        wmi = _wmi
    except ImportError:
        wmi = None
    try:
        import winreg as _winreg
        winreg = _winreg
    except ImportError:
        winreg = None


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "agent_state.json")
LOG_PATH = os.path.join(HERE, "agent.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("agente")


# ---------------------------------------------------------------------------
# Configuracao e identidade da maquina
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "server_url": "http://localhost:8000/api/ingest",
    "api_key": "troque-esta-chave",
    "interval_seconds": 3600,
    "tags": {"setor": "", "responsavel": ""},
    "verify_tls": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        log.warning("config.json nao encontrado; usando valores padrao.")
    except Exception as e:
        log.error("Falha lendo config.json: %s", e)
    return cfg


def _safe(fn, default=None):
    """Executa um coletor e nunca deixa a excecao vazar."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.debug("coletor %s falhou: %s", getattr(fn, "__name__", fn), e)
        return default


def stable_asset_id() -> str:
    """
    ID estavel por maquina. Prioridade: serial da BIOS + MAC primario.
    Persistido em agent_state.json para sobreviver a trocas de hardware
    perifericas. Se nada disso existir, gera um UUID e guarda.
    """
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f).get("asset_id")
            if saved:
                return saved
    except Exception:
        pass

    seed_parts = [platform.node()]
    serial = _safe(_bios_serial)
    if serial:
        seed_parts.append(serial)
    mac = uuid.getnode()
    seed_parts.append(str(mac))
    seed = "|".join(seed_parts)
    asset_id = hashlib.sha256(seed.encode()).hexdigest()[:24]

    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"asset_id": asset_id, "created": _now_iso()}, f)
    except Exception as e:
        log.debug("nao consegui persistir asset_id: %s", e)
    return asset_id


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Coletores - Identificacao / Hardware
# ---------------------------------------------------------------------------
def _bios_serial():
    if IS_WINDOWS and wmi:
        c = wmi.WMI()
        for b in c.Win32_BIOS():
            if b.SerialNumber:
                return b.SerialNumber.strip()
    if IS_LINUX:
        for p in ("/sys/class/dmi/id/product_serial",
                  "/sys/class/dmi/id/board_serial"):
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        v = f.read().strip()
                        if v:
                            return v
                except PermissionError:
                    pass
    if IS_MAC:
        out = subprocess.check_output(
            ["ioreg", "-l"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "IOPlatformSerialNumber" in line:
                return line.split('"')[-2]
    return None


def collect_system():
    data = {
        "hostname": platform.node(),
        "fqdn": _safe(socket.getfqdn),
        "os_name": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "os_full": " ".join(_safe(platform.uname, ()) or ()),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "bios_serial": _safe(_bios_serial),
        "boot_time": None,
        "logged_user": _safe(lambda: os.getlogin()),
        "domain": None,
        "manufacturer": None,
        "model": None,
    }
    if psutil:
        bt = _safe(psutil.boot_time)
        if bt:
            data["boot_time"] = dt.datetime.fromtimestamp(
                bt, dt.timezone.utc).isoformat()
            data["uptime_seconds"] = int(time.time() - bt)

    if IS_WINDOWS and wmi:
        c = wmi.WMI()
        for cs in _safe(lambda: c.Win32_ComputerSystem()) or []:
            data["manufacturer"] = cs.Manufacturer
            data["model"] = cs.Model
            data["domain"] = cs.Domain
            data["logged_user"] = cs.UserName or data["logged_user"]
            data["total_ram_bytes"] = int(cs.TotalPhysicalMemory or 0)
        for osw in _safe(lambda: c.Win32_OperatingSystem()) or []:
            data["os_caption"] = osw.Caption
            data["os_install_date"] = osw.InstallDate
            data["os_serial"] = osw.SerialNumber
    return data


def collect_hardware():
    hw = {"cpu": {}, "memory": {}, "disks": [], "gpu": []}
    if psutil:
        hw["cpu"] = {
            "cores_physical": _safe(lambda: psutil.cpu_count(logical=False)),
            "cores_logical": _safe(lambda: psutil.cpu_count(logical=True)),
            "freq_mhz": _safe(lambda: round(psutil.cpu_freq().max)
                              if psutil.cpu_freq() else None),
        }
        vm = _safe(psutil.virtual_memory)
        if vm:
            hw["memory"] = {"total_bytes": vm.total, "available_bytes": vm.available}

    if IS_WINDOWS and wmi:
        c = wmi.WMI()
        for p in _safe(lambda: c.Win32_Processor()) or []:
            hw["cpu"]["name"] = p.Name.strip() if p.Name else None
        for gpu in _safe(lambda: c.Win32_VideoController()) or []:
            hw["gpu"].append({"name": gpu.Name,
                              "driver": gpu.DriverVersion,
                              "vram_bytes": int(gpu.AdapterRAM or 0)})
        for d in _safe(lambda: c.Win32_DiskDrive()) or []:
            hw["disks"].append({
                "model": d.Model, "serial": (d.SerialNumber or "").strip(),
                "size_bytes": int(d.Size or 0),
                "interface": d.InterfaceType, "status": d.Status,
                "media_type": d.MediaType,
            })
    return hw


def collect_storage_usage():
    parts = []
    if not psutil:
        return parts
    for p in _safe(psutil.disk_partitions, []) or []:
        try:
            u = psutil.disk_usage(p.mountpoint)
            parts.append({
                "device": p.device, "mountpoint": p.mountpoint,
                "fstype": p.fstype, "opts": p.opts,
                "total_bytes": u.total, "used_bytes": u.used,
                "percent": u.percent,
                "removable": "removable" in (p.opts or "").lower(),
            })
        except (PermissionError, OSError):
            continue
    return parts


# ---------------------------------------------------------------------------
# Coletores - Rede
# ---------------------------------------------------------------------------
def collect_network():
    net = {"interfaces": [], "gateways": {}, "public_hint": None}
    if psutil:
        addrs = _safe(psutil.net_if_addrs, {}) or {}
        stats = _safe(psutil.net_if_stats, {}) or {}
        for name, addr_list in addrs.items():
            iface = {"name": name, "up": bool(stats.get(name) and stats[name].isup),
                     "ipv4": [], "ipv6": [], "mac": None}
            for a in addr_list:
                fam = getattr(a, "family", None)
                if fam == socket.AF_INET:
                    iface["ipv4"].append(a.address)
                elif fam == socket.AF_INET6:
                    iface["ipv6"].append(a.address)
                elif str(fam).endswith("AF_LINK") or str(fam).endswith("AF_PACKET"):
                    iface["mac"] = a.address
            net["interfaces"].append(iface)
    return net


# ---------------------------------------------------------------------------
# Coletores - Software instalado
# ---------------------------------------------------------------------------
def _win_installed_software():
    """Le as chaves de desinstalacao do registro (rapido e seguro).
    Evita Win32_Product, que dispara reconfiguracao MSI e e lento."""
    apps = []
    if not winreg:
        return apps
    roots = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    seen = set()
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except FileNotFoundError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                name = winreg.QueryValueEx(sub, "DisplayName")[0]
            except (OSError, FileNotFoundError):
                continue
            if not name or name in seen:
                continue
            seen.add(name)

            def _get(v):
                try:
                    return winreg.QueryValueEx(sub, v)[0]
                except OSError:
                    return None
            apps.append({
                "name": name,
                "version": _get("DisplayVersion"),
                "publisher": _get("Publisher"),
                "install_date": _get("InstallDate"),
            })
    return sorted(apps, key=lambda a: (a["name"] or "").lower())


def _unix_installed_software():
    cmds = [
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"],   # Debian/Ubuntu
        ["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}\n"],       # RHEL/Fedora
    ]
    if IS_MAC:
        cmds = [["ls", "/Applications"]]
    for cmd in cmds:
        try:
            out = subprocess.check_output(cmd, text=True,
                                          stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
        apps = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            apps.append({"name": parts[0].replace(".app", ""),
                         "version": parts[1] if len(parts) > 1 else None})
        return apps
    return []


def collect_software():
    if IS_WINDOWS:
        return _safe(_win_installed_software, []) or []
    return _safe(_unix_installed_software, []) or []


def collect_windows_patches():
    if not (IS_WINDOWS and wmi):
        return []
    c = wmi.WMI()
    return [{"id": h.HotFixID, "description": h.Description,
             "installed_on": h.InstalledOn}
            for h in (_safe(lambda: c.Win32_QuickFixEngineering()) or [])]


# ---------------------------------------------------------------------------
# Coletores - Dispositivos conectados (USB, monitores, impressoras)
# ---------------------------------------------------------------------------
def collect_connected_devices():
    dev = {"usb": [], "monitors": [], "printers": [], "removable_drives": []}

    if IS_WINDOWS and wmi:
        c = wmi.WMI()
        for e in _safe(lambda: c.Win32_PnPEntity()) or []:
            pnp = e.PNPDeviceID or ""
            if pnp.startswith("USB"):
                dev["usb"].append({"name": e.Name,
                                   "manufacturer": e.Manufacturer,
                                   "device_id": pnp, "status": e.Status})
        for m in _safe(lambda: c.Win32_DesktopMonitor()) or []:
            dev["monitors"].append({"name": m.Name,
                                    "screen_w": m.ScreenWidth,
                                    "screen_h": m.ScreenHeight})
        for pr in _safe(lambda: c.Win32_Printer()) or []:
            dev["printers"].append({"name": pr.Name, "port": pr.PortName,
                                    "default": pr.Default,
                                    "network": pr.Network})
        # Nomes/seriais reais dos monitores ficam em root\wmi
        try:
            w = wmi.WMI(namespace="root\\wmi")
            for mon in w.WmiMonitorID():
                def _dec(arr):
                    return "".join(chr(x) for x in arr if x) if arr else None
                dev["monitors"].append({
                    "product": _dec(mon.UserFriendlyName),
                    "serial": _dec(mon.SerialNumberID),
                    "manufacturer": _dec(mon.ManufacturerName),
                })
        except Exception:
            pass

    if IS_LINUX:
        out = _safe(lambda: subprocess.check_output(
            ["lsusb"], text=True, stderr=subprocess.DEVNULL))
        if out:
            for line in out.strip().splitlines():
                dev["usb"].append({"name": line})

    # Drives removiveis (pen drives, HDs externos) via psutil, todos os SO
    for part in collect_storage_usage():
        if part.get("removable"):
            dev["removable_drives"].append(part)
    return dev


# ---------------------------------------------------------------------------
# Coletores - Saude / status operacional
# ---------------------------------------------------------------------------
def collect_health():
    h = {}
    if psutil:
        h["cpu_percent"] = _safe(lambda: psutil.cpu_percent(interval=1.0))
        vm = _safe(psutil.virtual_memory)
        if vm:
            h["memory_percent"] = vm.percent
        batt = _safe(psutil.sensors_battery)
        if batt:
            h["battery_percent"] = batt.percent
            h["battery_plugged"] = batt.power_plugged
        temps = _safe(psutil.sensors_temperatures)
        if temps:
            flat = [t.current for arr in temps.values() for t in arr if t.current]
            if flat:
                h["temp_max_c"] = max(flat)
    # pior uso de disco entre as particoes fixas
    disks = [d["percent"] for d in collect_storage_usage()
             if not d.get("removable")]
    if disks:
        h["disk_worst_percent"] = max(disks)
    h["pending_reboot"] = _safe(_win_pending_reboot) if IS_WINDOWS else None
    h["antivirus"] = _safe(_win_antivirus) if IS_WINDOWS else None
    return h


def _win_pending_reboot():
    if not winreg:
        return None
    checks = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"),
    ]
    for hive, path in checks:
        try:
            winreg.OpenKey(hive, path)
            return True
        except FileNotFoundError:
            continue
    return False


def _win_antivirus():
    """Le o Windows Security Center. Mostra status do Kaspersky/Defender
    sem tocar no antivirus (somente leitura)."""
    if not wmi:
        return None
    try:
        w = wmi.WMI(namespace="root\\SecurityCenter2")
    except Exception:
        return None
    result = []
    for av in w.AntiVirusProduct():
        # productState e um bitfield; decodificacao resumida
        state = int(av.productState)
        enabled = bool(state & 0x1000)
        up_to_date = not bool(state & 0x10)
        result.append({"name": av.displayName,
                       "enabled": enabled,
                       "up_to_date": up_to_date})
    return result


# ---------------------------------------------------------------------------
# Montagem do payload e envio
# ---------------------------------------------------------------------------
def build_payload(cfg: dict) -> dict:
    return {
        "asset_id": stable_asset_id(),
        "collected_at": _now_iso(),
        "agent_version": "1.0.0",
        "tags": cfg.get("tags", {}),
        "system": _safe(collect_system, {}),
        "hardware": _safe(collect_hardware, {}),
        "storage": _safe(collect_storage_usage, []),
        "network": _safe(collect_network, {}),
        "software": _safe(collect_software, []),
        "patches": _safe(collect_windows_patches, []),
        "devices": _safe(collect_connected_devices, {}),
        "health": _safe(collect_health, {}),
    }


def build_partial_payload(cfg: dict) -> dict:
    logged_user = None
    try:
        logged_user = os.getlogin()
    except Exception:
        pass
    if IS_WINDOWS and wmi:
        try:
            c = wmi.WMI()
            for cs in c.Win32_ComputerSystem():
                if cs.UserName:
                    logged_user = cs.UserName
        except Exception:
            pass

    return {
        "asset_id": stable_asset_id(),
        "collected_at": _now_iso(),
        "partial": True,
        "system": {
            "hostname": socket.gethostname(),
            "logged_user": logged_user
        },
        "health": _safe(collect_health, {}),
    }


def check_for_updates(cfg: dict) -> bool:
    if not requests:
        return False
    try:
        server_url = cfg.get("server_url", "")
        if not server_url:
            return False
        # Remove a rota para obter o base_url do servidor
        server_base = server_url.split("/api/ingest")[0].rstrip("/")
        agent_url = f"{server_base}/api/agent/download/agent.py"

        # Faz o download do código atual no servidor
        r = requests.get(agent_url, timeout=15, verify=cfg.get("verify_tls", True))
        if r.status_code != 200:
            return False

        new_code = r.text
        local_path = os.path.abspath(__file__)
        with open(local_path, "r", encoding="utf-8") as f:
            current_code = f.read()

        current_hash = hashlib.sha256(current_code.encode("utf-8")).hexdigest()
        new_hash = hashlib.sha256(new_code.encode("utf-8")).hexdigest()

        if current_hash != new_hash:
            log.info("Nova versao do agente detectada no servidor! Atualizando...")
            # Faz backup do arquivo atual antes de atualizar
            backup_path = local_path + ".bak"
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(current_code)

            try:
                # Sobrescreve com o novo código
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(new_code)
                log.info("Agente atualizado com sucesso. Reiniciando processo...")
                # Remove backup antes de reiniciar
                if os.path.exists(backup_path):
                    try:
                        os.remove(backup_path)
                    except Exception:
                        pass
                # Executa o processo novamente substituindo o atual
                os.execv(sys.executable, [sys.executable] + sys.argv)
                return True
            except Exception as e:
                log.error("Falha ao sobrescrever o arquivo do agente: %s. Restaurando backup...", e)
                if os.path.exists(backup_path):
                    try:
                        os.replace(backup_path, local_path)
                    except Exception:
                        pass
        else:
            log.debug("Agente ja esta na versao mais recente.")
    except Exception as e:
        log.error("Falha verificando atualizacoes do agente: %s", e)
    return False


def send_payload(cfg: dict, payload: dict) -> bool:
    if not requests:
        log.error("Biblioteca 'requests' ausente. pip install requests")
        return False
    try:
        r = requests.post(
            cfg["server_url"],
            json=payload,
            headers={"X-API-Key": cfg.get("api_key", ""),
                     "Content-Type": "application/json"},
            timeout=30,
            verify=cfg.get("verify_tls", True),
        )
        if r.status_code < 300:
            log.info("Enviado OK (%s) asset=%s", r.status_code,
                     payload["asset_id"])
            return True
        log.error("Servidor respondeu %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.error("Falha no envio: %s", e)
    return False


def run_once(cfg, dry_run=False, partial=False):
    if partial:
        payload = build_partial_payload(cfg)
    else:
        payload = build_payload(cfg)
    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return True
    return send_payload(cfg, payload)


def main():
    ap = argparse.ArgumentParser(description="Agente de inventario de TI")
    ap.add_argument("--loop", action="store_true",
                    help="roda continuamente no intervalo do config")
    ap.add_argument("--dry-run", action="store_true",
                    help="coleta e imprime o JSON, sem enviar")
    args = ap.parse_args()

    if psutil is None:
        log.warning("psutil ausente: coleta limitada. pip install psutil")

    cfg = load_config()
    if not args.dry_run:
        # Checa por atualizações logo no arranque
        check_for_updates(cfg)

    if not args.loop:
        ok = run_once(cfg, dry_run=args.dry_run, partial=False)
        sys.exit(0 if ok else 1)

    interval = int(cfg.get("interval_seconds", 3600))
    fast_interval = 15
    log.info("Agente em laco. Intervalo do Inventario=%ss. Intervalo Tempo Real=%ss", interval, fast_interval)

    # Executa primeiro envio completo no arranque
    run_once(cfg, dry_run=args.dry_run, partial=False)
    last_full_scan = time.time()

    while True:
        time.sleep(fast_interval)
        now = time.time()
        if now - last_full_scan >= interval:
            if not args.dry_run:
                # Checa por atualizações antes de iniciar a varredura completa
                check_for_updates(cfg)
            log.info("Executando inventario completo agendado...")
            run_once(cfg, dry_run=args.dry_run, partial=False)
            last_full_scan = now
        else:
            run_once(cfg, dry_run=args.dry_run, partial=True)


if __name__ == "__main__":
    main()
