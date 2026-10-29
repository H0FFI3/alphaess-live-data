"""
AlphaESS PV-Inverter Live Dashboard Server
Liest Modbus-Daten sekündlich und streamt sie per WebSocket an das Dashboard.
"""

import asyncio
import json
import os
import struct
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from collections import deque
from pathlib import Path
from dotenv import load_dotenv

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pyModbusTCP.client import ModbusClient

# .env laden
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# --- KONFIGURATION AUS .ENV ---
INVERTER_IP = os.getenv("INVERTER_IP")
PORT = int(os.getenv("PORT", os.getenv("MODBUS_PORT")))
SLAVE_ID = int(os.getenv("SLAVE_ID"))
TOTAL_BATTERY_CAPACITY_KWH = float(os.getenv("TOTAL_BATTERY_CAPACITY_KWH"))
REGISTER_JSON = BASE_DIR / os.getenv("REGISTER_JSON")

SERVER_HOST = os.getenv("SERVER_HOST")
SERVER_PORT = int(os.getenv("SERVER_PORT"))
# ------------------------------

# Maximale Anzahl an Datenpunkten im RAM (24 Stunden × 3600 Sekunden)
MAX_HISTORY = 86400

# Thread-Lock für Modbus-Kommunikation
modbus_lock = threading.Lock()

# Cache für die aktuellen Live-Werte der Register
latest_register_values: dict = {}

def load_register_defs() -> list:
    """Lädt die Definitionen aller verfügbaren Register aus register.json."""
    if not REGISTER_JSON.exists():
        return []
    try:
        data = json.loads(REGISTER_JSON.read_text(encoding="utf-8"))
        available = data.get("available_registers", {})
        defs = []
        for reg in available.values():
            try:
                defs.append({
                    "address": int(reg["address"]),
                    "type": str(reg.get("type", "uint16be")),
                    "len": int(reg.get("len", 1)),
                    "factor": str(reg.get("factor", "1")),
                    "offset": str(reg.get("offset", "0")),
                })
            except (ValueError, TypeError):
                continue
        defs.sort(key=lambda x: x["address"])
        return defs
    except Exception as e:
        print(f"[Server] Fehler beim Laden von register.json: {e}")
        return []

register_defs = load_register_defs()

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(modbus_loop())
    asyncio.create_task(register_polling_loop())
    yield

app = FastAPI(title="AlphaESS Live Dashboard", lifespan=lifespan)

# In-Memory-Ringpuffer für die letzten 60 Datenpunkte
history = deque(maxlen=MAX_HISTORY)

# Verbundene WebSocket-Clients
clients: set[WebSocket] = set()

# Clients, die sich aktuell im "Register"-Tab befinden
register_tab_clients: set[WebSocket] = set()

# Globaler Modbus-Client
modbus_client: ModbusClient | None = None


def create_modbus_client() -> ModbusClient:
    return ModbusClient(
        host=INVERTER_IP, port=PORT, unit_id=SLAVE_ID, auto_open=True
    )


def read_int32(regs):
    return struct.unpack(">i", struct.pack(">HH", regs[0], regs[1]))[0]

def read_uint32(regs):
    return struct.unpack(">I", struct.pack(">HH", regs[0], regs[1]))[0]

def read_int16(regs):
    return struct.unpack(">h", struct.pack(">H", regs[0]))[0]


def read_single_register(client: ModbusClient, address: int, reg_type: str, length: int):
    regs = client.read_holding_registers(address, length)
    if regs is None:
        return None

    if reg_type == "uint16be":
        return regs[0]
    elif reg_type == "int16be":
        raw = struct.pack(">H", regs[0])
        return struct.unpack(">h", raw)[0]
    elif reg_type == "uint32be":
        if len(regs) < 2: return None
        raw = struct.pack(">HH", regs[0], regs[1])
        return struct.unpack(">I", raw)[0]
    elif reg_type == "int32be":
        if len(regs) < 2: return None
        raw = struct.pack(">HH", regs[0], regs[1])
        return struct.unpack(">i", raw)[0]
    elif reg_type == "string":
        chars = []
        for r in regs:
            high = (r >> 8) & 0xFF
            low = r & 0xFF
            if 32 <= high <= 126:
                chars.append(chr(high))
            if 32 <= low <= 126:
                chars.append(chr(low))
        return "".join(chars).strip()
    return regs[0]


def apply_factor_offset(raw_value, factor_str: str, offset_str: str):
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        return raw_value
    try:
        factor = float(str(factor_str).replace(",", "."))
        val = raw_value * factor
    except (ValueError, TypeError):
        val = raw_value
    try:
        offset = float(str(offset_str).replace(",", "."))
        val += offset
    except (ValueError, TypeError):
        pass
    if isinstance(val, float):
        if val.is_integer():
            return int(val)
        return round(val, 4)
    return val


def read_all_registers(client: ModbusClient, defs: list) -> dict:
    """Liest alle definierten Register nacheinander mit kurzem Thread-Locking."""
    results = {}
    for reg in defs:
        addr = reg["address"]
        try:
            with modbus_lock:
                raw = read_single_register(client, addr, reg["type"], reg["len"])
            if raw is not None:
                scaled = apply_factor_offset(raw, reg["factor"], reg["offset"])
                results[str(addr)] = {
                    "raw": raw,
                    "scaled": scaled
                }
        except Exception:
            pass
        # 5ms Pause für verzögerungsfreie Ausführung des 1s-Telemetrie-Loops
        time.sleep(0.005)
    return results


def read_inverter_data(client: ModbusClient) -> dict | None:
    """Liest alle relevanten Register vom Wechselrichter."""
    try:
        with modbus_lock:
            # ── Leistungs-Register ──
            regs_grid      = client.read_holding_registers(33,   2)  # Total Active Power Grid (int32, W)
            regs_pv1       = client.read_holding_registers(1055, 2)  # PV1 Power (int32, W)
            regs_pv2       = client.read_holding_registers(1059, 2)  # PV2 Power (int32, W)
            regs_pv3       = client.read_holding_registers(1063, 2)  # PV3 Power (int32, W)
            regs_bat       = client.read_holding_registers(294,  1)  # Battery Power (int16, W)
            regs_soc       = client.read_holding_registers(258,  1)  # Battery SOC (uint16, % × 0.1)

            # ── Batterie-Detail-Register ──
            regs_bat_cap   = client.read_holding_registers(281,  1)  # Battery capacity (uint16, kWh × 0.1)
            regs_bat_soh   = client.read_holding_registers(283,  1)  # Battery SOH (uint16, % × 0.1)
            regs_bat_chg   = client.read_holding_registers(288,  2)  # Battery charge energy (uint32, kWh × 0.1)
            regs_bat_dchg  = client.read_holding_registers(290,  2)  # Battery discharge energy (uint32, kWh × 0.1)
            regs_bat_grid  = client.read_holding_registers(292,  2)  # Battery energy charge from grid (uint32, kWh × 0.1)
            regs_max_soc   = client.read_holding_registers(2133, 1)  # Max Battery SOC (uint16, %)

            # ── PV Energie-Zähler ──
            regs_total_pv  = client.read_holding_registers(1086, 2)  # Total PV Energy (uint32, kWh × 0.1)
            regs_pv_inv    = client.read_holding_registers(2256, 2)  # PV Inverter energy (uint32, kWh × 0.1)

        # Aktuelle PV-Leistung aus PV1+PV2+PV3 berechnen
        pv1_power = read_int32(regs_pv1) if regs_pv1 else 0
        pv2_power = read_int32(regs_pv2) if regs_pv2 else 0
        pv3_power = read_int32(regs_pv3) if regs_pv3 else 0
        if pv1_power < 0: pv1_power = 0
        if pv2_power < 0: pv2_power = 0
        if pv3_power < 0: pv3_power = 0
        pv_power = pv1_power + pv2_power + pv3_power

        if not regs_grid or not regs_bat or not regs_soc:
            return None

        # Netzleistung (32-bit signed, W)
        grid_power = read_int32(regs_grid)

        # Batterieleistung (16-bit signed, W)
        bat_power = read_int16(regs_bat)
        bat_discharge = bat_power if bat_power > 0 else 0

        # SoC (Rohwert × 0.1 = %)
        bat_soc = regs_soc[0] * 0.1

        # Energie im Speicher
        energy_kwh = (bat_soc / 100.0) * TOTAL_BATTERY_CAPACITY_KWH

        # Hausverbrauch
        house_load = pv_power + bat_power - grid_power
        if house_load < 0:
            house_load = 0

        # Optionale Batterie-Werte
        bat_capacity  = round(regs_bat_cap[0]  * 0.1, 2) if regs_bat_cap  else None
        bat_soh       = round(regs_bat_soh[0]  * 0.1, 1) if regs_bat_soh  else None
        bat_chg_kwh   = round(read_uint32(regs_bat_chg)  * 0.1, 1) if regs_bat_chg  else None
        bat_dchg_kwh  = round(read_uint32(regs_bat_dchg) * 0.1, 1) if regs_bat_dchg else None
        bat_grid_kwh  = round(read_uint32(regs_bat_grid) * 0.1, 1) if regs_bat_grid else None
        max_soc       = regs_max_soc[0]                              if regs_max_soc  else None
        total_pv_kwh  = round(read_uint32(regs_total_pv) * 0.1, 1)  if regs_total_pv else None
        pv_inv_kwh    = round(read_uint32(regs_pv_inv)   * 0.1, 1)  if regs_pv_inv   else None

        return {
            "timestamp":    datetime.now().isoformat(),
            "time":         datetime.now().strftime("%H:%M:%S"),
            "grid_power":   grid_power,
            "pv_power":     pv_power,
            "pv1_power":    pv1_power,
            "pv2_power":    pv2_power,
            "pv3_power":    pv3_power,
            "house_load":   house_load,
            "bat_power":    bat_power,
            "bat_discharge": bat_discharge,
            "bat_soc":      round(bat_soc, 1),
            "energy_kwh":   round(energy_kwh, 2),
            "total_capacity_kwh": TOTAL_BATTERY_CAPACITY_KWH,
            # Neue Felder
            "bat_capacity":   bat_capacity,
            "bat_soh":        bat_soh,
            "bat_chg_kwh":    bat_chg_kwh,
            "bat_dchg_kwh":   bat_dchg_kwh,
            "bat_grid_kwh":   bat_grid_kwh,
            "max_soc":        max_soc,
            "total_pv_kwh":   total_pv_kwh,
            "pv_inv_kwh":     pv_inv_kwh,
        }
    except Exception as e:
        print(f"Fehler beim Lesen der Modbus-Register: {e}")
        return None


async def broadcast(data: dict):
    """Sendet Daten an alle verbundenen WebSocket-Clients."""
    import json
    dead = set()
    message = json.dumps(data)
    for ws in list(clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)
    register_tab_clients.difference_update(dead)


async def broadcast_registers(data: dict):
    """Sendet Register-Updates nur an Clients, die den Tab 'register' geöffnet haben."""
    if not register_tab_clients:
        return
    dead = set()
    message = json.dumps(data)
    for ws in list(register_tab_clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    register_tab_clients.difference_update(dead)
    clients.difference_update(dead)


async def modbus_loop():
    """Asynchrone Endlosschleife: Liest Haupt-Dashboard-Daten exakt sekündlich und broadcastet sie."""
    global modbus_client
    with modbus_lock:
        modbus_client = create_modbus_client()
    print(f"[Server] Verbunden mit Inverter {INVERTER_IP}:{PORT} (Unit-ID: {SLAVE_ID})")

    while True:
        t0 = time.time()
        data = await asyncio.get_event_loop().run_in_executor(None, read_inverter_data, modbus_client)
        if data:
            history.append(data)
            await broadcast({"type": "update", "data": data})
        else:
            # Verbindung neu aufbauen
            print("[Server] Verbindungsfehler – versuche Reconnect...")
            with modbus_lock:
                try:
                    modbus_client.close()
                except Exception:
                    pass
            await asyncio.sleep(3)
            with modbus_lock:
                modbus_client = create_modbus_client()
            continue

        # 5-Sekunden-Takt für das Haupt-Dashboard
        elapsed = time.time() - t0
        sleep_time = max(0.1, 5.0 - elapsed)
        await asyncio.sleep(sleep_time)


async def register_polling_loop():
    """Liest alle Register alle 5 Sekunden – aber NUR wenn ein Client im Register-Tab ist!"""
    global latest_register_values
    await asyncio.sleep(1)  # Kurze Verzögerung beim Serverstart

    # Einmalige Initialauslesung beim Start, damit Seriennummer & SW-Versionen sofort live vorliegen
    if modbus_client and register_defs:
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, read_all_registers, modbus_client, register_defs
            )
            if data:
                ts = datetime.now().strftime("%H:%M:%S")
                latest_register_values = {
                    "timestamp": ts,
                    "values": data
                }
                print(f"[Register] Initiale Live-Werte (inkl. Seriennummer) erfolgreich ausgelesen.")
        except Exception as e:
            print(f"[Register] Fehler beim initialen Auslesen: {e}")

    while True:
        try:
            # Nur zyklisch auslesen, wenn mindestens ein Benutzer gerade den Register-Tab geöffnet hat
            if register_tab_clients and modbus_client and register_defs:
                data = await asyncio.get_event_loop().run_in_executor(
                    None, read_all_registers, modbus_client, register_defs
                )
                if data:
                    ts = datetime.now().strftime("%H:%M:%S")
                    latest_register_values = {
                        "timestamp": ts,
                        "values": data
                    }
                    await broadcast_registers({
                        "type": "registers_live",
                        "timestamp": ts,
                        "values": data
                    })
        except Exception as e:
            print(f"[Register Loop] Fehler beim zyklischen Auslesen: {e}")

        # Alle 5 Sekunden abfragen
        await asyncio.sleep(5)



@app.get("/api/history")
async def get_history():
    """Gibt den gespeicherten Verlauf der letzten 24 Stunden zurück."""
    return {"history": list(history)}


@app.get("/api/registers")
async def get_registers():
    """Gibt die verfügbaren Register aus register.json zurück (mit aktuellen Live-Werten angereichert)."""
    if REGISTER_JSON.exists():
        data = json.loads(REGISTER_JSON.read_text(encoding="utf-8"))
        # Wenn bereits Live-Werte (z. B. Seriennummer) vorliegen, direkt einpflegen
        if latest_register_values and "values" in latest_register_values:
            live = latest_register_values["values"]
            for key, reg in data.get("available_registers", {}).items():
                addr_str = str(reg.get("address"))
                if addr_str in live:
                    reg["raw_value"] = live[addr_str].get("raw")
                    reg["scaled_value"] = live[addr_str].get("scaled")
        return JSONResponse(content=data)
    return JSONResponse(content={"error": "register.json nicht gefunden"}, status_code=404)


@app.get("/api/registers/live")
async def get_live_registers():
    """Gibt die aktuellen 5-Sekunden Live-Werte der Register zurück."""
    return latest_register_values or {"timestamp": "", "values": {}}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket-Endpoint für Live-Updates."""
    import json
    await websocket.accept()
    clients.add(websocket)
    # Sofort den aktuellen Verlauf für das Haupt-Dashboard schicken
    if history:
        await websocket.send_text(json.dumps({"type": "history", "data": list(history)}))

    try:
        while True:
            text = await websocket.receive_text()
            try:
                msg = json.loads(text)
                if msg.get("type") == "view_tab":
                    if msg.get("tab") == "registers":
                        register_tab_clients.add(websocket)
                        # Wenn bereits Live-Registerdaten vorliegen, sofort zusenden
                        if latest_register_values:
                            await websocket.send_text(json.dumps({
                                "type": "registers_live",
                                "timestamp": latest_register_values.get("timestamp", ""),
                                "values": latest_register_values.get("values", {})
                            }))
                    else:
                        register_tab_clients.discard(websocket)
            except Exception:
                pass
    except WebSocketDisconnect:
        clients.discard(websocket)
        register_tab_clients.discard(websocket)


# Statische Dateien (Dashboard HTML/CSS/JS)
dashboard_dir = Path(__file__).parent / "dashboard"
dashboard_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")


if __name__ == "__main__":
    print("=" * 60)
    print("  AlphaESS PV-Inverter Live Dashboard")
    print(f"  Starte Server auf http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"  Starte Server auf http://localhost:{SERVER_PORT}")
    print("=" * 60)
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
