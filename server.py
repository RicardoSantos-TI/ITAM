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
from fastapi.responses import HTMLResponse, JSONResponse

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

    with db() as conn:
        conn.execute(
            """INSERT INTO assets (asset_id, hostname, last_seen, data)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                 hostname=excluded.hostname,
                 last_seen=excluded.last_seen,
                 data=excluded.data""",
            (asset_id, hostname, now_iso(), json.dumps(payload)),
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


# Inicialização assíncrona/em segundo plano do banco de dados para evitar atrasar o arranque
# e otimizar o tempo de resposta inicial (pronto para Azure/Railway/etc.)
threading.Thread(target=init_db, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    # Permite iniciar o servidor diretamente com `python server.py`
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)

