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
    API_KEY   chave que o agente precisa enviar no header X-API-Key
    DB_PATH   caminho do arquivo SQLite (padrao: inventario.db)
"""

import json
import os
import sqlite3
import datetime as dt
from contextlib import contextmanager
import threading

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse

API_KEY = os.environ.get("API_KEY", "troque-esta-chave-por-uma-forte")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__),
                                                 "inventario.db"))
DASHBOARD = os.path.join(os.path.dirname(__file__), "dashboard.html")

app = FastAPI(title="Inventario de TI", version="1.0.0")


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
            """
        )


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def get_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{proto}://{host}/"



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
    return JSONResponse({"ok": True, "asset_id": asset_id})


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------
def _status_of(payload: dict, last_seen: str) -> str:
    """Deriva o status geral do ativo: ok / atencao / critico / offline."""
    try:
        seen = dt.datetime.fromisoformat(last_seen)
        if (dt.datetime.now(dt.timezone.utc) - seen).total_seconds() > 86400:
            return "offline"
    except Exception:
        pass
    h = payload.get("health") or {}
    disk = h.get("disk_worst_percent") or 0
    mem = h.get("memory_percent") or 0
    cpu = h.get("cpu_percent") or 0
    if disk >= 92 or mem >= 95:
        return "critico"
    if disk >= 80 or mem >= 85 or cpu >= 90 or h.get("pending_reboot"):
        return "atencao"
    return "ok"


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
            out.append({
                "asset_id": row["asset_id"],
                "hostname": row["hostname"],
                "last_seen": row["last_seen"],
                "status": _status_of(payload, row["last_seen"]),
                "os": sysinfo.get("os_caption") or sysinfo.get("os_full"),
                "model": sysinfo.get("model"),
                "manufacturer": sysinfo.get("manufacturer"),
                "domain": sysinfo.get("domain"),
                "user": sysinfo.get("logged_user"),
                "cpu": health.get("cpu_percent"),
                "memory": health.get("memory_percent"),
                "disk": health.get("disk_worst_percent"),
                "software_count": len(payload.get("software") or []),
                "tags": payload.get("tags") or {},
            })
    return out


@app.get("/api/assets/{asset_id}")
def asset_detail(asset_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT data, last_seen FROM assets WHERE asset_id=?",
            (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ativo nao encontrado")
        payload = json.loads(row["data"])
        payload["status"] = _status_of(payload, row["last_seen"])
        hist = conn.execute(
            "SELECT ts, cpu, memory, disk FROM history "
            "WHERE asset_id=? ORDER BY ts DESC LIMIT 100",
            (asset_id,)).fetchall()
        payload["history"] = [dict(h) for h in reversed(hist)]
    return payload


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
def dashboard():
    try:
        with open(DASHBOARD, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>dashboard.html nao encontrado</h1>"


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

# 5. Criar tarefa agendada para rodar de hora em hora
Write-Host "Configurando Tarefa Agendada no Windows..." -ForegroundColor Yellow

$taskName = "ITAM-Agent"
$pythonw = Get-Command pythonw -ErrorAction SilentlyContinue
if (-not $pythonw) {{
    $pythonwPath = "pythonw.exe"
}} else {{
    $pythonwPath = $pythonw.Source
}}

# Remove tarefa existente se houver
Register-ScheduledTask -TaskName $taskName -Action (New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c echo") -Trigger (New-ScheduledTaskTrigger -At (Get-Date) -Once) -Force | Out-Null
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "$pythonwPath" -Argument "\\`"$agentDir\\agent.py --loop\\`""
$trigger = New-ScheduledTaskTrigger -AtLogon
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User "NT AUTHORITY\\SYSTEM" -Force | Out-Null

Write-Host "`n================================================" -ForegroundColor Cyan
Write-Host "✅ AGENTE INSTALADO E CONFIGURADO COM SUCESSO!" -ForegroundColor Green
Write-Host "O agente irá rodar continuamente em segundo plano (Tempo Real)." -ForegroundColor White
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
echo "✅ Python3 encontrado: \\$(python3 --version)"

# 2. Criar diretório do agente
AGENT_DIR="/opt/itam-agent"
sudo mkdir -p "\\$AGENT_DIR"
sudo chmod 755 "\\$AGENT_DIR"

# 3. Baixar arquivos
echo "Baixando arquivos do agente..."
sudo curl -sS -o "\\$AGENT_DIR/agent.py" "{base_url}/api/agent/download/agent.py"
sudo curl -sS -o "\\$AGENT_DIR/config.json" "{base_url}/api/agent/download/config.json"
sudo chmod 644 "\\$AGENT_DIR/agent.py" "\\$AGENT_DIR/config.json"

# 4. Instalar dependências
echo "Instalando dependências do Python..."
sudo python3 -m pip install --upgrade pip -q || true
sudo pip3 install psutil requests || sudo python3 -m pip install psutil requests || true

# 5. Adicionar no cron para rodar a cada hora
echo "Configurando Cron Job..."
CRON_JOB="0 * * * * python3 \\$AGENT_DIR/agent.py >/dev/null 2>&1"
(sudo crontab -l 2>/dev/null | grep -Fv "\\$AGENT_DIR/agent.py"; echo "\\$CRON_JOB") | sudo crontab -

echo
echo "================================================"
echo "✅ AGENTE INSTALADO E CONFIGURADO COM SUCESSO!"
echo "O agente irá rodar automaticamente a cada hora."
echo "================================================"
echo

# Executar a primeira coleta
echo "Executando primeira coleta de dados agora..."
sudo python3 "\\$AGENT_DIR/agent.py"
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

