from pyModbusTCP.client import ModbusClient
import struct
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

# Ihre exakte Batterie-Gesamtkapazität in kWh
TOTAL_BATTERY_CAPACITY_KWH = float(os.getenv("TOTAL_BATTERY_CAPACITY_KWH", "7.68"))
# ------------------------------

# Client initialisieren
client = ModbusClient(
    host=INVERTER_IP, port=PORT, unit_id=SLAVE_ID, auto_open=True
)

print(f"Verbunden mit Inverter {INVERTER_IP}:{PORT} (Unit-ID: {SLAVE_ID})")
print(f"Starte sekundengenaues Auslesen mit {TOTAL_BATTERY_CAPACITY_KWH} kWh Batteriespeicher...\n")

# Tabellen-Header
print(
    f"{'Zeit':<10} | {'Netz (W)':<10} | {'Solar (W)':<10} | {'Last (W)':<10}"
    f" | {'Abgeb. Batterie':<15} | {'Batterie-Power':<15} | {'SoC (%)':<8}"
    f" | {'Energie (kWh)':<13}"
)
print("-" * 118)

try:
  while True:
    current_time = time.strftime("%H:%M:%S")

    # 1. Netzleistung auslesen (Register 33, 2 Register = 32-bit)
    regs_grid = client.read_holding_registers(33, 2)

    # 2. Solarenergie / PV Total Power auslesen (Register 1107, 2 Register = 32-bit)
    regs_pv = client.read_holding_registers(1107, 2)

    # 3. Batterieleistung auslesen (Register 294, 1 Register = 16-bit signed)
    regs_bat = client.read_holding_registers(294, 1)

    # 4. Batterieladestand / SoC auslesen (Register 258, 1 Register = 16-bit)
    regs_soc = client.read_holding_registers(258, 1)

    if regs_grid and regs_pv and regs_bat and regs_soc:
      # Netzleistung (32-bit)
      grid_power = struct.unpack(
          ">i", struct.pack(">HH", regs_grid[0], regs_grid[1])
      )[0]

      # Solarenergie (32-bit)
      pv_power = struct.unpack(
          ">i", struct.pack(">HH", regs_pv[0], regs_pv[1])
      )[0]
      if pv_power < 0:
        pv_power = 0

      # Batterieleistung (16-bit signed short)
      raw_bat = regs_bat[0]
      bat_power = struct.unpack(">h", struct.pack(">H", raw_bat))[0]

      # Abgegebene Batterieleistung (nur Entladung anzeigen, sonst 0)
      bat_discharge = bat_power if bat_power > 0 else 0

      # SoC: Rohwert durch 10 teilen
      bat_soc = regs_soc[0] / 10.0

      # Aktuell im Speicher vorhandene Energie in kWh basierend auf Gesamtkapazität berechnen
      energy_kwh = (bat_soc / 100.0) * TOTAL_BATTERY_CAPACITY_KWH

      # Hausverbrauch (Last) berechnen:
      house_load = pv_power + bat_power - grid_power
      if house_load < 0:
        house_load = 0

      print(
          f"{current_time:<10} | {grid_power:9d} W | {pv_power:9d} W |"
          f" {house_load:9d} W | {bat_discharge:15d} W | {bat_power:15d} W |"
          f" {bat_soc:5.1f} % | {energy_kwh:10.2f} kWh"
      )
    else:
      print(f"{current_time:<10} | Fehler beim Lesen der Modbus-Register")

    time.sleep(1)

except KeyboardInterrupt:
  print("\nSkript wird beendet...")
  client.close()
