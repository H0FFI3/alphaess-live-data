from pyModbusTCP.client import ModbusClient
import struct
import json
import time
import os
from pathlib import Path
from dotenv import load_dotenv

# .env laden
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# --- KONFIGURATION AUS .ENV ---
INVERTER_IP = os.getenv("INVERTER_IP")
PORT = int(os.getenv("PORT", os.getenv("MODBUS_PORT")))
SLAVE_ID = int(os.getenv("SLAVE_ID"))
REGISTER_FILE = BASE_DIR / os.getenv("REGISTER_FILE")
OUTPUT_FILE = BASE_DIR / os.getenv("OUTPUT_FILE", os.getenv("REGISTER_JSON"))
# ------------------------------

def parse_register_file(filepath):
    """Parsed die holding_register.txt und gibt eine Liste von Register-Definitionen zurück."""
    registers = []
    with open(filepath, encoding="utf-8") as f:
        lines = f.readlines()

    # Erste Zeile ist der Header
    header = lines[0].strip().split("\t")
    for line in lines[1:]:
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        entry = {}
        for i, col in enumerate(header):
            entry[col] = parts[i] if i < len(parts) else ""
        registers.append(entry)
    return registers


def read_register(client, address, reg_type, length):
    """Liest ein oder mehrere Register und gibt den Rohwert als Integer zurück."""
    regs = client.read_holding_registers(address, length)
    if regs is None:
        return None

    if reg_type in ("uint16be",):
        return regs[0]

    elif reg_type in ("int16be",):
        raw = struct.pack(">H", regs[0])
        return struct.unpack(">h", raw)[0]

    elif reg_type in ("uint32be",):
        if len(regs) < 2:
            return None
        raw = struct.pack(">HH", regs[0], regs[1])
        return struct.unpack(">I", raw)[0]

    elif reg_type in ("int32be",):
        if len(regs) < 2:
            return None
        raw = struct.pack(">HH", regs[0], regs[1])
        return struct.unpack(">i", raw)[0]

    elif reg_type == "string":
        chars = []
        for r in regs:
            high = (r >> 8) & 0xFF
            low = r & 0xFF
            if high:
                chars.append(chr(high))
            if low:
                chars.append(chr(low))
        return "".join(chars).strip("\x00").strip()

    else:
        # Fallback: uint16
        return regs[0]


def apply_factor(raw_value, factor_str):
    """Wendet den Faktor aus der Registerdefinition an."""
    try:
        factor = float(factor_str.replace(",", "."))
        return raw_value * factor
    except (ValueError, TypeError):
        return raw_value


def main():
    registers = parse_register_file(REGISTER_FILE)
    print(f"Geladene Register-Definitionen: {len(registers)}")

    client = ModbusClient(
        host=INVERTER_IP, port=PORT, unit_id=SLAVE_ID, auto_open=True
    )
    print(f"Verbinde mit {INVERTER_IP}:{PORT} (Unit-ID: {SLAVE_ID})...\n")

    available = {}
    failed = []

    total = len(registers)
    for idx, reg in enumerate(registers):
        address_str = reg.get("_address", "").strip()
        name        = reg.get("name", "").strip()
        reg_type    = reg.get("type", "uint16be").strip()
        unit        = reg.get("unit", "").strip()
        description = reg.get("description", "").strip()
        factor_str  = reg.get("factor", "1").strip()
        offset_str  = reg.get("offset", "0").strip()
        role        = reg.get("role", "").strip()
        length_str  = reg.get("len", "1").strip()

        try:
            address = int(address_str)
        except ValueError:
            print(f"  [SKIP] Ungültige Adresse: '{address_str}' ({name})")
            continue

        try:
            length = int(length_str)
        except ValueError:
            length = 1

        # Kurze Pause um den Inverter nicht zu überlasten
        time.sleep(0.05)

        raw_value = read_register(client, address, reg_type, length)

        if raw_value is not None:
            scaled = apply_factor(raw_value, factor_str)
            try:
                offset = float(offset_str.replace(",", "."))
                scaled += offset
            except (ValueError, TypeError):
                pass

            key = f"{address}_{name}" if name else str(address)
            available[key] = {
                "address":     address,
                "name":        name,
                "description": description,
                "unit":        unit,
                "type":        reg_type,
                "len":         length,
                "factor":      factor_str,
                "offset":      offset_str,
                "role":        role,
                "raw_value":   raw_value if not isinstance(raw_value, str) else raw_value,
                "scaled_value": round(scaled, 4) if isinstance(scaled, float) else scaled,
            }
            print(
                f"[{idx+1:3d}/{total}] OK  | Addr {address:5d} | {name:<45} | "
                f"raw={raw_value}  skaliert={scaled} {unit}"
            )
        else:
            failed.append({
                "address":     address,
                "name":        name,
                "description": description,
            })
            print(
                f"[{idx+1:3d}/{total}] FAIL| Addr {address:5d} | {name}"
            )

    client.close()

    # Ergebnis als JSON speichern
    output = {
        "meta": {
            "inverter_ip": INVERTER_IP,
            "port":        PORT,
            "slave_id":    SLAVE_ID,
            "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_defined":   total,
            "total_available": len(available),
            "total_failed":    len(failed),
        },
        "available_registers": available,
        "failed_registers":    failed,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Fertig! {len(available)} verfügbar, {len(failed)} nicht lesbar.")
    print(f"   Ergebnis gespeichert in: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

