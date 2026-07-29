#!/usr/bin/env python3
"""
Servidor de Inventario de TI
----------------------------
Recebe os envios do agente, guarda o snapshot atual e um historico
de saude por ativo, e serve o dashboard.

Rodar:
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8000

Config por variaveis de ambiente:
    API_KEY                chave que o agente envia no header X-API-Key
    DB_PATH                caminho do arquivo SQLite (padrao: inventario.db)
    OFFLINE_AFTER_SECONDS  sem envios por esse tempo => ativo "offline".
                           Padrao: 120s (24 batimentos perdidos do modo
                           tempo-real de 5s). Se alguma maquina usar so o
                           cron horario, suba para 7200.
"""

import json
import os
import sqlite3
import datetime as dt
import urllib.request
import urllib.error
from contextlib import contextmanager
import threading

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse

API_KEY = os.environ.get("API_KEY", "troque-esta-chave-por-uma-forte")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__),
                                                 "inventario.db"))
OFFLINE_AFTER_SECONDS = int(os.environ.get("OFFLINE_AFTER_SECONDS", "120"))

# --- Geolocalizacao por Wi-Fi (WPS) ---------------------------------------
# Desligado por padrao. Defina GEO_PROVIDER para ativar:
#   "google"   -> usa Google Geolocation API (precisa de GEO_API_KEY)
#   "beacondb" -> usa BeaconDB (gratuito/comunitario, cobertura irregular)
GEO_PROVIDER = os.environ.get("GEO_PROVIDER", "").strip().lower()
GEO_API_KEY = os.environ.get("GEO_API_KEY", "")

DASHBOARD = os.path.join(os.path.dirname(__file__), "dashboard.html")
ATIVO = os.path.join(os.path.dirname(__file__), "ativo.html")

app = FastAPI(title="Inventario de TI", version="1.2.1")


# ---------------------------------------------------------------------------
# Banco (SQLite, sem ORM para manter dependencias minimas)
# ---------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                asset_id   TEXT PRIMARY KEY,
                hostname   TEXT,
                last_seen  TEXT,
                data       TEXT
            );
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id   TEXT,
                ts         TEXT,
                cpu        REAL,
                memory     REAL,
                disk       REAL,
                snapshot   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_history_asset
                ON history (asset_id, ts);
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                asset_id   TEXT NOT NULL,
                action     TEXT NOT NULL,
                status     TEXT NOT NULL,
                output     TEXT,
                created_at TEXT NOT NULL,
                executed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_commands_asset
                ON commands (asset_id, status);
            """
        )


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def get_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{proto}://{host}/"


def client_ip_of(request: Request):
    """IP de origem do envio. Atras de proxy/TLS (Caddy, Nginx, Railway),
    o IP real do agente chega no X-Forwarded-For (primeiro da lista)."""
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd
    return request.client.host if request.client else None


def geolocate_wifi(aps: list) -> dict | None:
    """Converte uma lista de BSSIDs [{bssid, signal_dbm, channel}] em
    {lat, lng, accuracy, provider} chamando o provedor configurado.
    Retorna None se WPS estiver desligado, sem APs, ou em caso de falha."""
    if not GEO_PROVIDER or not aps:
        return None

    wifi = []
    for ap in aps:
        b = ap.get("bssid")
        if not b:
            continue
        entry = {"macAddress": b}
        if ap.get("signal_dbm") is not None:
            entry["signalStrength"] = ap["signal_dbm"]
        if ap.get("channel") is not None:
            entry["channel"] = ap["channel"]
        wifi.append(entry)
    if len(wifi) < 2:  # 1 unico AP raramente posiciona bem
        return None

    if GEO_PROVIDER == "google":
        url = ("https://www.googleapis.com/geolocation/v1/geolocate?key="
               + GEO_API_KEY)
    elif GEO_PROVIDER == "beacondb":
        url = "https://api.beacondb.net/v1/geolocate"
    else:
        return None

    body = json.dumps({"considerIp": False, "wifiAccessPoints": wifi}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        loc = data.get("location") or {}
        if loc.get("lat") is None:
            return None
        return {
            "lat": loc.get("lat"),
            "lng": loc.get("lng"),
            "accuracy": data.get("accuracy"),
            "provider": GEO_PROVIDER,
            "located_at": now_iso(),
            "ap_count": len(wifi),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError):
        return None



# ---------------------------------------------------------------------------
# Ingestao
# ---------------------------------------------------------------------------
@app.post("/api/ingest")
async def ingest(request: Request, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="chave invalida")

    payload = await request.json()
    asset_id = payload.get("asset_id")
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id ausente")

    hostname = (payload.get("system") or {}).get("hostname")
    health = payload.get("health") or {}
    is_partial = payload.get("partial", False)
    ingest_meta = {"source_ip": client_ip_of(request), "received_at": now_iso()}

    # 1. Processar resultado de comando enviado pelo agente (se houver)
    cmd_result = payload.get("command_result")
    if cmd_result:
        cmd_id = cmd_result.get("id")
        cmd_status = cmd_result.get("status")  # "success" ou "failed"
        cmd_output = cmd_result.get("output")
        with db() as conn:
            conn.execute(
                "UPDATE commands SET status=?, output=?, executed_at=? WHERE command_id=?",
                (cmd_status, cmd_output, now_iso(), cmd_id)
            )

    with db() as conn:
        if is_partial:
            # Tenta buscar o inventário existente para mesclar
            row = conn.execute("SELECT hostname, data FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
            if row:
                existing_data = json.loads(row["data"])
                # Mescla a saúde
                existing_data["health"] = health
                # Atualiza o timestamp
                existing_data["timestamp"] = payload.get("timestamp") or now_iso()
                # Atualiza informações dinâmicas de sistema (como usuário logado)
                if "system" in payload and payload["system"]:
                    if "system" not in existing_data:
                        existing_data["system"] = {}
                    existing_data["system"]["logged_user"] = payload["system"].get("logged_user") or existing_data["system"].get("logged_user")

                payload_to_save = existing_data
                hostname = row["hostname"] or hostname
            else:
                payload_to_save = payload
        else:
            payload_to_save = payload

        # Metadados de recepcao (IP de origem para geolocalizacao no dashboard)
        payload_to_save["_ingest"] = ingest_meta

        conn.execute(
            """INSERT INTO assets (asset_id, hostname, last_seen, data)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                 hostname=excluded.hostname,
                 last_seen=excluded.last_seen,
                 data=excluded.data""",
            (asset_id, hostname, now_iso(), json.dumps(payload_to_save)),
        )
        conn.execute(
            """INSERT INTO history (asset_id, ts, cpu, memory, disk, snapshot)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (asset_id, now_iso(),
             health.get("cpu_percent"),
             health.get("memory_percent"),
             health.get("disk_worst_percent"),
             json.dumps(health)),
        )

    # 2. Buscar comandos pendentes para este agente
    pending_cmd = None
    with db() as conn:
        row = conn.execute(
            "SELECT command_id, action FROM commands WHERE asset_id=? AND status='pending' ORDER BY created_at ASC LIMIT 1",
            (asset_id,)
        ).fetchone()
        if row:
            pending_cmd = {"command_id": row["command_id"], "action": row["action"]}
            # Marca como "running" para nao reenviar enquanto executa
            conn.execute(
                "UPDATE commands SET status='running' WHERE command_id=?",
                (row["command_id"],)
            )

    return JSONResponse({"ok": True, "asset_id": asset_id, "command": pending_cmd})


@app.post("/api/assets/{asset_id}/command")
async def queue_command(asset_id: str, request: Request):
    body = await request.json()
    action = body.get("action")
    if not action:
        raise HTTPException(status_code=400, detail="action ausente")

    import uuid
    cmd_id = str(uuid.uuid4())

    with db() as conn:
        conn.execute(
            "INSERT INTO commands (command_id, asset_id, action, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (cmd_id, asset_id, action, now_iso())
        )
    return {"ok": True, "command_id": cmd_id, "status": "pending"}


@app.get("/api/assets/{asset_id}/command/{command_id}")
def get_command_status(asset_id: str, command_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT status, output, created_at, executed_at FROM commands WHERE command_id=? AND asset_id=?",
            (command_id, asset_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="comando nao encontrado")
        return {
            "command_id": command_id,
            "status": row["status"],
            "output": row["output"],
            "created_at": row["created_at"],
            "executed_at": row["executed_at"]
        }


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------
def _seconds_since(last_seen: str):
    """Segundos desde o ultimo envio, ou None se a data for invalida."""
    try:
        seen = dt.datetime.fromisoformat(last_seen)
        return (dt.datetime.now(dt.timezone.utc) - seen).total_seconds()
    except Exception:
        return None


def _is_online(last_seen: str) -> bool:
    s = _seconds_since(last_seen)
    return s is not None and s <= OFFLINE_AFTER_SECONDS


def _status_of(payload: dict, last_seen: str) -> str:
    """Deriva o status geral do ativo: ok / atencao / critico / offline."""
    if not _is_online(last_seen):
        return "offline"
    h = payload.get("health") or {}
    disk = h.get("disk_worst_percent") or 0
    mem = h.get("memory_percent") or 0
    cpu = h.get("cpu_percent") or 0
    if disk >= 92 or mem >= 95:
        return "critico"
    if disk >= 80 or mem >= 85 or cpu >= 90 or h.get("pending_reboot"):
        return "atencao"
    return "ok"


def get_user_department(logged_user: str) -> str | None:
    if not logged_user:
        return None
    user_clean = logged_user.lower().strip()
    if "\\" in user_clean:
        user_clean = user_clean.split("\\")[-1]

    csv_path = os.path.join(os.path.dirname(__file__), "usersSP_2026-7-29.csv")
    if not os.path.exists(csv_path):
        csv_path = os.path.join(os.path.dirname(__file__), "fido-usuarios-s_o_paulo-s_o_paulo-all-2026-07-29.csv")
        if not os.path.exists(csv_path):
            return None

    try:
        import csv
        with open(csv_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                upn = row.get("userPrincipalName") or row.get("UPN")
                dept = row.get("department")
                if upn and dept:
                    upn_clean = upn.lower().strip()
                    if upn_clean.startswith(f"{user_clean}@"):
                        return dept.strip()
    except Exception as e:
        print(f"Erro ao ler CSV de departamentos: {e}")
    return None


@app.get("/api/assets")
def list_assets():
    out = []
    with db() as conn:
        for row in conn.execute(
                "SELECT asset_id, hostname, last_seen, data FROM assets "
                "ORDER BY hostname"):
            payload = json.loads(row["data"])
            sysinfo = payload.get("system") or {}
            health = payload.get("health") or {}
            secs = _seconds_since(row["last_seen"])

            tags = payload.get("tags") or {}
            logged_user = sysinfo.get("logged_user")
            dept = get_user_department(logged_user)
            if dept:
                tags = dict(tags)
                tags["setor"] = dept

            out.append({
                "asset_id": row["asset_id"],
                "hostname": row["hostname"],
                "last_seen": row["last_seen"],
                "status": _status_of(payload, row["last_seen"]),
                "online": _is_online(row["last_seen"]),
                "seconds_since_seen": round(secs) if secs is not None else None,
                "source_ip": (payload.get("_ingest") or {}).get("source_ip"),
                "os": sysinfo.get("os_caption") or sysinfo.get("os_full"),
                "model": sysinfo.get("model"),
                "manufacturer": sysinfo.get("manufacturer"),
                "domain": sysinfo.get("domain"),
                "user": sysinfo.get("logged_user"),
                "cpu": health.get("cpu_percent"),
                "memory": health.get("memory_percent"),
                "disk": health.get("disk_worst_percent"),
                "software_count": len(payload.get("software") or []),
                "tags": tags,
                "agent_version": payload.get("agent_version") or "1.0.0",
            })
    return out


@app.get("/api/assets/{asset_id}")
def asset_detail(asset_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT hostname, data, last_seen FROM assets WHERE asset_id=?",
            (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ativo nao encontrado")
        payload = json.loads(row["data"])
        secs = _seconds_since(row["last_seen"])
        payload["hostname"] = row["hostname"]
        payload["last_seen"] = row["last_seen"]
        payload["status"] = _status_of(payload, row["last_seen"])
        payload["online"] = _is_online(row["last_seen"])
        payload["seconds_since_seen"] = round(secs) if secs is not None else None
        payload["source_ip"] = (payload.get("_ingest") or {}).get("source_ip")
        payload["wifi_ap_count"] = len(payload.get("wifi_scan") or [])
        payload["wps_enabled"] = bool(GEO_PROVIDER)

        # Resolve setor pelo departamento do usuario
        sysinfo = payload.get("system") or {}
        logged_user = sysinfo.get("logged_user")
        dept = get_user_department(logged_user)
        if dept:
            if "tags" not in payload:
                payload["tags"] = {}
            payload["tags"]["setor"] = dept

        # Nao devolve a lista bruta de BSSIDs ao frontend (dado sensivel);
        # so a contagem e a ultima posicao ja calculada, se houver.
        payload.pop("wifi_scan", None)
        hist = conn.execute(
            "SELECT ts, cpu, memory, disk FROM history "
            "WHERE asset_id=? ORDER BY ts DESC LIMIT 100",
            (asset_id,)).fetchall()
        payload["history"] = [dict(h) for h in reversed(hist)]
    return payload


@app.post("/api/assets/{asset_id}/locate")
def locate_asset(asset_id: str):
    """Geolocaliza o ativo por Wi-Fi (WPS) sob demanda, usando os BSSIDs
    do ultimo inventario. Requer GEO_PROVIDER configurado. Guarda o
    resultado em _geo para exibicao no mapa."""
    if not GEO_PROVIDER:
        raise HTTPException(status_code=503,
                            detail="WPS desligado. Defina GEO_PROVIDER no servidor.")
    with db() as conn:
        row = conn.execute(
            "SELECT data FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ativo nao encontrado")
        payload = json.loads(row["data"])
        aps = payload.get("wifi_scan") or []
        if not aps:
            raise HTTPException(
                status_code=422,
                detail="Sem redes Wi-Fi no ultimo inventario (maquina cabeada "
                       "ou sem adaptador Wi-Fi).")
        geo = geolocate_wifi(aps)
        if not geo:
            raise HTTPException(
                status_code=502,
                detail="Provedor nao retornou posicao (cobertura insuficiente "
                       "ou falha na chamada).")
        payload["_geo"] = geo
        conn.execute("UPDATE assets SET data=? WHERE asset_id=?",
                     (json.dumps(payload), asset_id))
    return geo


@app.get("/api/summary")
def summary():
    counts = {"ok": 0, "atencao": 0, "critico": 0, "offline": 0}
    total = 0
    with db() as conn:
        for row in conn.execute("SELECT data, last_seen FROM assets"):
            payload = json.loads(row["data"])
            counts[_status_of(payload, row["last_seen"])] += 1
            total += 1
    return {"total": total, **counts}


# ---------------------------------------------------------------------------
# Dashboard & Detalhes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard.html", response_class=HTMLResponse)
def dashboard():
    try:
        with open(DASHBOARD, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>dashboard.html nao encontrado</h1>"


@app.get("/ativo.html", response_class=HTMLResponse)
def ativo_pagina():
    try:
        with open(ATIVO, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>ativo.html nao encontrado</h1>"


@app.get("/ativo-detalhes.html", response_class=HTMLResponse)
def ativo_detalhes():
    try:
        detalhes_path = os.path.join(os.path.dirname(__file__), "ativo-detalhes.html")
        with open(detalhes_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>ativo-detalhes.html nao encontrado</h1>"


@app.get("/instalar.html", response_class=HTMLResponse)
def instalar_pagina():
    try:
        instalar_path = os.path.join(os.path.dirname(__file__), "instalar.html")
        with open(instalar_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>instalar.html nao encontrado</h1>"


@app.get("/api/agent/download/agent.py")
def download_agent():
    agent_path = os.path.join(os.path.dirname(__file__), "agent.py")
    if not os.path.exists(agent_path):
        raise HTTPException(status_code=404, detail="agent.py nao encontrado")
    return FileResponse(agent_path, media_type="text/plain", filename="agent.py")


@app.get("/api/agent/download/config.json")
def download_config(request: Request):
    # Retorna o config pre-configurado com a URL deste servidor
    server_url = f"{get_base_url(request)}api/ingest"
    cfg = {
        "server_url": server_url,
        "api_key": API_KEY,
        "interval_seconds": 3600,
        "realtime_interval_seconds": 5,
        "tags": {
            "setor": "Default",
            "responsavel": ""
        },
        "verify_tls": True
    }
    return JSONResponse(content=cfg)


@app.get("/api/agent/installer.ps1", response_class=PlainTextResponse)
def get_installer_ps1(request: Request):
    base_url = get_base_url(request).rstrip("/")
    script = f"""# ITAM Agent Installer for Windows
$ErrorActionPreference = "Stop"

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Instalador do Agente ITAM - Windows" -ForegroundColor Green
Write-Host "================================================`n" -ForegroundColor Cyan

# 1. Verificar/Instalar Python
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {{
    Write-Host "Python não encontrado. Instalando Python via winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3 --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {{
        Write-Host "❌ Erro ao instalar Python via winget. Por favor, instale o Python manualmente (https://python.org) e tente novamente." -ForegroundColor Red
        exit 1
    }}
    Write-Host "✅ Python instalado com sucesso. Reiniciando caminho de variáveis..." -ForegroundColor Green
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}} else {{
    Write-Host "✅ Python já instalado: $(python --version)" -ForegroundColor Green
}}

# 2. Criar diretório do agente
$agentDir = "C:\\Program Files\\ITAM-Agent"
if (-not (Test-Path $agentDir)) {{
    New-Item -ItemType Directory -Force -Path $agentDir | Out-Null
}}

# 3. Baixar arquivos
Write-Host "Baixando arquivos do agente..." -ForegroundColor Yellow
$agentUrl = "{base_url}/api/agent/download/agent.py"
$configUrl = "{base_url}/api/agent/download/config.json"

Invoke-WebRequest -Uri $agentUrl -OutFile "$agentDir\\agent.py" -UseBasicParsing
Invoke-WebRequest -Uri $configUrl -OutFile "$agentDir\\config.json" -UseBasicParsing

# 4. Instalar dependências
Write-Host "Instalando dependências do Python..." -ForegroundColor Yellow
& python -m pip install --upgrade pip -q
& pip install -q psutil requests WMI pywin32
if ($LASTEXITCODE -ne 0) {{
    Write-Host "⚠️ Aviso ao instalar dependências. Tentando novamente sem modo silencioso..." -ForegroundColor Yellow
    & pip install psutil requests WMI pywin32
}}

# 5. Criar tarefa agendada: inicia junto com o SISTEMA (boot) e continua em loop
Write-Host "Configurando Tarefa Agendada no Windows (inicialização do sistema)..." -ForegroundColor Yellow

$taskName = "ITAM-Agent"
$pythonw = Get-Command pythonw -ErrorAction SilentlyContinue
if (-not $pythonw) {{
    $pythonwPath = "pythonw.exe"
}} else {{
    $pythonwPath = $pythonw.Source
}}

# Remove tarefa existente se houver
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "$pythonwPath" -Argument "\\`"$agentDir\\agent.py\\`" --loop"

# Dispara no BOOT do sistema (antes mesmo de qualquer logon) e também no logon,
# como redundância caso a tarefa seja parada manualmente.
$triggerBoot  = New-ScheduledTaskTrigger -AtStartup
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn

# Sem limite de tempo de execução (loop infinito) + reinício automático se cair
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($triggerBoot, $triggerLogon) -Settings $settings `
    -User "NT AUTHORITY\\SYSTEM" -RunLevel Highest -Force | Out-Null

Write-Host "`n================================================" -ForegroundColor Cyan
Write-Host "✅ AGENTE INSTALADO E CONFIGURADO COM SUCESSO!" -ForegroundColor Green
Write-Host "Inicia automaticamente com o Windows e envia dados em tempo real (5s)." -ForegroundColor White
Write-Host "================================================" -ForegroundColor Cyan

# Executar a primeira coleta imediatamente para registrar o ativo
Write-Host "Executando primeira coleta de dados agora..." -ForegroundColor Yellow
& python "$agentDir\\agent.py"
Write-Host "✅ Coleta de registro concluída!" -ForegroundColor Green

# Iniciar o serviço em tempo real em segundo plano imediatamente
Start-ScheduledTask -TaskName $taskName
"""
    return PlainTextResponse(content=script, media_type="text/plain")


@app.get("/api/agent/installer.sh", response_class=PlainTextResponse)
def get_installer_sh(request: Request):
    base_url = get_base_url(request).rstrip("/")
    script = f"""#!/bin/bash
set -e

echo "================================================"
echo "  Instalador do Agente ITAM - Linux / macOS"
echo "================================================"
echo

# 1. Verificar Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python3 não encontrado! Por favor, instale o Python3 e tente novamente."
    exit 1
fi
echo "✅ Python3 encontrado: $(python3 --version)"

# 2. Criar diretório do agente
AGENT_DIR="/opt/itam-agent"
sudo mkdir -p "$AGENT_DIR"
sudo chmod 755 "$AGENT_DIR"

# 3. Baixar arquivos
echo "Baixando arquivos do agente..."
sudo curl -sS -o "$AGENT_DIR/agent.py" "{base_url}/api/agent/download/agent.py"
sudo curl -sS -o "$AGENT_DIR/config.json" "{base_url}/api/agent/download/config.json"
sudo chmod 644 "$AGENT_DIR/agent.py" "$AGENT_DIR/config.json"

# 4. Instalar dependências
echo "Instalando dependências do Python..."
sudo python3 -m pip install --upgrade pip -q || true
sudo pip3 install psutil requests || sudo python3 -m pip install psutil requests || true

# 5. Registrar para iniciar junto com o sistema, em loop tempo-real
PYTHON_BIN="$(command -v python3)"

if command -v systemctl &>/dev/null; then
    echo "Configurando serviço systemd (inicia no boot, loop tempo-real)..."
    sudo tee /etc/systemd/system/itam-agent.service > /dev/null <<UNIT
[Unit]
Description=Agente de Inventario de TI (ITAM)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN $AGENT_DIR/agent.py --loop
WorkingDirectory=$AGENT_DIR
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now itam-agent.service
    echo "✅ Serviço itam-agent ativo (systemctl status itam-agent)."

elif [ "$(uname)" = "Darwin" ]; then
    echo "Configurando LaunchDaemon do macOS (inicia no boot, loop tempo-real)..."
    sudo tee /Library/LaunchDaemons/io.inventar.agent.plist > /dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.inventar.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$AGENT_DIR/agent.py</string>
    <string>--loop</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$AGENT_DIR</string>
</dict>
</plist>
PLIST
    sudo launchctl load -w /Library/LaunchDaemons/io.inventar.agent.plist
    echo "✅ LaunchDaemon io.inventar.agent carregado."

else
    echo "systemd não encontrado; usando cron @reboot como alternativa..."
    CRON_JOB="@reboot $PYTHON_BIN $AGENT_DIR/agent.py --loop >/dev/null 2>&1"
    (sudo crontab -l 2>/dev/null | grep -Fv "$AGENT_DIR/agent.py"; echo "$CRON_JOB") | sudo crontab -
    sudo nohup "$PYTHON_BIN" "$AGENT_DIR/agent.py" --loop >/dev/null 2>&1 &
    echo "✅ Cron @reboot registrado e agente iniciado em segundo plano."
fi

echo
echo "================================================"
echo "✅ AGENTE INSTALADO E CONFIGURADO COM SUCESSO!"
echo "Inicia automaticamente com o sistema e envia dados em tempo real (5s)."
echo "================================================"
echo

# Executar a primeira coleta completa para registrar o ativo imediatamente
echo "Executando primeira coleta de dados agora..."
sudo python3 "$AGENT_DIR/agent.py"
echo "✅ Coleta concluída e dados enviados!"
"""
    return PlainTextResponse(content=script, media_type="text/plain")


# Inicialização assíncrona/em segundo plano do banco de dados para evitar atrasar o arranque
# e otimizar o tempo de resposta inicial (pronto para Azure/Railway/etc.)
threading.Thread(target=init_db, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    # Permite iniciar o servidor diretamente com `python server.py`
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)