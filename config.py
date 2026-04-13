"""Konfiguration für DentalHKP Anwendung"""
import os
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "solutiodb"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Projektordner: Docker-Volume oder lokaler Pfad
PROJEKTE_PFAD = os.getenv("PROJEKTE_PFAD", "D:\\DentalProjekte")

# ─────────────────────────────────────────────────────────────────────────────
# Stücklisten-Katalog
# ─────────────────────────────────────────────────────────────────────────────
# USE_KATALOG=true  → Katalog als primäre Schablone nutzen (KI passt nur an)
# USE_KATALOG=false → Klassischer Modus: KI-Agent schlägt alles selbst vor
USE_KATALOG = os.getenv("USE_KATALOG", "true").lower() == "true"

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# Vollständiger Behandlungscode-Katalog (Position 1-2 im befund01pa zahn-String)
#
# Nur die 11 Codes die überhaupt als NEU GEPLANT vorkommen (pos 24='2').
# Alle anderen ~90 "exotischen" Codes sind IST-Befunde (High-Bit 0x80 gesetzt)
# und werden durch den is_new_plan-Filter ohnehin ausgeschlossen.
#
# Ermittelt durch systematische DB-Analyse (April 2026):
#   Byte 2 (index 1): Kategorie – '0'=Einzelzahn, '8'=Teleskop/Anker, 'G'=Implantat
#   Byte 3 (index 2): Typ – '8'=VK, '4'=KK, '5'=VMK, '2'=Metall, 0xC0=Inlay, ...
# ─────────────────────────────────────────────────────────────────────────────
TOOTH_TREATMENT_CODES = {
    # ── Häufige Kronentypen (ASCII-Ziffern als Typbyte) ─────────────────────
    "08": "Keramikkrone (Vollkeramik)",          # 4504× – häufigster Code
    "05": "Verblendkrone (VMK / Metall-Keramik)",# 324×
    "02": "Metallkrone",                          # 170×
    "04": "Kunststoffkrone",                      # 129×
    "03": "Krone (Sondertyp)",                    # selten
    "09": "Krone (Sondertyp)",                    # selten

    # ── Inlay / Cerec (Byte 3 = 0xC0) ───────────────────────────────────────
    "0\xc0": "Inlay / Cerec-Restauration",        # 167× – fehlte bisher!
    #   Umfasst: I3-Cerec, Onlay, E.max, Zirkon-Restauration (indirekt)
    #   GOZ-Basis: 2200 (dreiflächig) als Default; Agent präzisiert

    # ── Metallkeramik-Sondertyp (Byte 3 = 0x65 = 'e') ───────────────────────
    "0e": "Metallkeramik / Onlay (Sondertyp)",    # 156× – MK-GOZ, Onlay
    #   Erscheint bei 'MK GOZ', 'MK Galvano' – ähnlich VMK aber anderer Code

    # ── Brückenglied Vollkeramik (Byte 3 = 0xBA) ─────────────────────────────
    "0\xba": "Vollkeramik-Brückenglied (Pontic)", # 125× – bei Vollkeramikbrücken

    # ── Teleskopkrone / Prothesen-Anker ──────────────────────────────────────
    "88": "Teleskopkrone / Prothesen-Anker",      # 137×
    "8e": "Teleskopkrone (Sondertyp)",            # 35×

    # ── Seltene / unklare Codes ──────────────────────────────────────────────
    "0b": "Krone / Restauration (Sondertyp)",     # 9×
    "0G": "Implantat-Aufbaukrone",                # 8× – G=Implantat als Typbyte
    "0W": "Implantat-Krone",                      # alt – selten

    # ── Legacycodes aus früherer Analyse ────────────────────────────────────
    "01": "Extraktion / Sonstige",
}

# Position 10 (0-indexed) im zahn-String: 'G' = Implantat-Träger
IMPLANT_FLAG_CHAR = "G"
IMPLANT_FLAG_POS  = 10

# Suffix für treatment_name wenn Implantat erkannt
IMPLANT_SUFFIX = " auf Implantat"

# GOZ-Basisposition je Kronentyp
GOZ_BASIS_KRONE = {
    "08": "2210",      # Vollkeramikkrone
    "04": "2210v",     # Kunststoffkrone
    "05": "2210",      # VMK/Verblendkrone
    "02": "2210",      # Metallkrone
    "03": "2210",      # Krone Sondertyp
    "09": "2210",      # Krone Sondertyp
    "88": "2210",      # Teleskopkrone (Hauptkrone)
    "8e": "2210",      # Teleskop Sondertyp
    "0\xc0": "2200",   # Inlay dreiflächig (default; Agent präzisiert auf 2180/2190/2200)
    "0e": "2210",      # MK-Sondertyp → Krone
    "0\xba": "2210",   # Brückenglied VK → gleicher GOZ wie Krone
    "0b": "2210",      # Sondertyp
    "0G": "2200i",     # Implantat-Aufbaukrone → wie Implantatkrone
    "0W": "2200i",     # Implantat-Krone (alt)
}

# GOZ-Basisposition wenn Implantat-Träger (überschreibt GOZ_BASIS_KRONE)
GOZ_BASIS_IMPLANTAT = "2200i"   # §6-Analog Implantatkrone

# ─────────────────────────────────────────────────────────────────────────────
# Session-GOZ: nur EINMAL pro Sitzung abrechenbar (nicht pro Zahn)
# ─────────────────────────────────────────────────────────────────────────────
GOZ_SESSION_EINMALIG = frozenset({
    # MKO-Paket (Mundhygiene/Kassen-Ost GOZ §1 Abs.2)
    "8000", "8010", "8020", "8030", "8040", "8050", "8060", "8070", "8080",
    # Untersuchung/Beratung
    "0010", "0030", "0040",   # Eingehende Untersuchung etc.
    "Ä1",                     # Beratung
})

# Praxisspezifisches Paket bei Kronenversorgungen (aus historischer Analyse)
GOZ_PFLICHT_BEI_KRONE = ["2030", "8000", "8010", "8020", "8060", "8080"]
GOZ_STANDARD_BEI_KRONE = ["5190a", "0040", "4050", "2270", "2120z", "Ä1", "5110a"]
GOZ_OPTIONAL_BEI_KRONE = ["0090", "0070", "0100", "2290"]

# Zusätzliche Positionen bei Implantat-Krone
GOZ_PFLICHT_BEI_IMPLANTAT  = ["2200i", "9050", "2197"]      # Implantatkrone, Abutment, Adhäsiv
GOZ_STANDARD_BEI_IMPLANTAT = ["5190a", "8000", "8010", "8020", "8060", "8080", "2030"]
GOZ_OPTIONAL_BEI_IMPLANTAT = ["5120i", "2270i", "19"]       # Prov. Implantatkrone etc.
