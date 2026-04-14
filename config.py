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
GOZ_PFLICHT_BEI_IMPLANTAT  = ["2200i", "9050", "2197"]
# ↑ "9050" = Charly-Praxisalias für "Abutment entfernen/einsetzen" (§6-Analog)
#   ACHTUNG: GOZ 2012 §9050 = Sinuslift extern – abweichende Verwendung in Charly!
GOZ_STANDARD_BEI_IMPLANTAT = ["5190a", "8000", "8010", "8020", "8060", "8080", "2030"]
GOZ_OPTIONAL_BEI_IMPLANTAT = ["5120i", "2270i", "19"]       # Prov. Implantatkrone etc.

# ─────────────────────────────────────────────────────────────────────────────
# Vollständige GOZ-Referenztabelle
# Format: "goz_nr": ("offizielle Leistungsbezeichnung", "kategorie")
#
# Verwendet von:
#   • hkp_agents.py  → kontextabhängige Injektion in den Agent-Prompt
#   • text_parser.py → Parser-Prompt anreichern
#
# Kategorien: allgemein | konservierend | inlay | krone | prothetik |
#             mko | chirurgie | implantologie | analog
# ─────────────────────────────────────────────────────────────────────────────
GOZ_REFERENZ: dict[str, tuple[str, str]] = {
    # ── Allgemeine Leistungen ────────────────────────────────────────────────
    "0010": ("Beratung", "allgemein"),
    "0030": ("Eingehende Untersuchung und Beratung", "allgemein"),
    "0040": ("Befundaufnahme und Behandlungsplanung", "allgemein"),
    "0050": ("Schriftlicher Heil- und Kostenplan", "allgemein"),
    "0060": ("Eingehende Untersuchung der Kaumuskulatur/Gelenke (CMD)", "allgemein"),
    "0070": ("Präventive Beratung", "allgemein"),
    "0090": ("Kurze Information (telefonisch / mündlich)", "allgemein"),
    "Ä1":   ("Eingehende Beratung (analog ärztl. GOÄ §3)", "allgemein"),

    # ── Konservierend / Füllungen ────────────────────────────────────────────
    "2030": ("Provisorische Füllung / Aufbau", "konservierend"),
    "2040": ("Adhäsive Befestigung provisorisch", "konservierend"),
    "2060": ("Kunststofffüllung 1-flächig", "konservierend"),
    "2080": ("Kunststofffüllung 2-flächig", "konservierend"),
    "2100": ("Kunststofffüllung 3-flächig", "konservierend"),
    "2120": ("Aufbaufüllung (Stumpfaufbau) direkt", "konservierend"),
    "2170": ("Einlagefüllung mehr als zweiflächig (praxisspez. Alias für Inlay 3-fl.)", "inlay"),
    "2180": ("Inlay / Einlagefüllung 1-flächig (Keramik oder Gold)", "inlay"),
    "2190": ("Inlay / Einlagefüllung 2-flächig", "inlay"),
    "2197": ("Adhäsive Befestigung (Inlay, Krone, Veneer, Brückenglied)", "inlay"),
    "2200": ("Inlay / Einlagefüllung 3-flächig und mehr", "inlay"),

    # ── Krone / Brücke ───────────────────────────────────────────────────────
    "2210": ("Krone (Vollgusskrone, Verblendkrone, Vollkeramikkrone)", "krone"),
    "2270": ("Stiftaufbau indirekt (Guss-/Keramikstift)", "krone"),
    "2290": ("Entfernung einer Krone / eines Inlays", "krone"),
    "2310": ("Brückenglied (gegossenes Metall)", "krone"),
    "2320": ("Brückenglied (vollkeramisch)", "krone"),

    # ── Prothetik ────────────────────────────────────────────────────────────
    "5000": ("Planung Gesamtprothetik / Erstuntersuchung", "prothetik"),
    "5110": ("Vorübergehende Krone (Langzeit-Provisorium)", "prothetik"),
    "5120": ("Vorübergehende Krone auf Implantat (Provisorium)", "prothetik"),
    "5140": ("Adhäsiv-Kurzzeit-Provisorium", "prothetik"),
    "5190": ("Abformung mit individuellem Löffel (je Kiefer)", "prothetik"),

    # ── MKO-Paket (einmalig pro Sitzung) ────────────────────────────────────
    "8000": ("MKO – Kofferdam / Spanngummi anlegen", "mko"),
    "8010": ("MKO – Assistenz / Trockenlegung", "mko"),
    "8020": ("MKO – Farbfotodokumentation", "mko"),
    "8030": ("MKO – Okklusionsregistrierung", "mko"),
    "8040": ("MKO – Materialvorbereitung / Bonding-Protokoll", "mko"),
    "8050": ("MKO – Medikamentöse Einlage / Unterfüllung", "mko"),
    "8060": ("MKO – Ergänzende Maßnahmen (Licht, Lagerung)", "mko"),
    "8070": ("MKO – Anästhesieprotokoll / Vitalitätsprüfung", "mko"),
    "8080": ("MKO – Materialaufwand / Sonderinstrumentarium", "mko"),

    # ── Chirurgie – Extraktion / Osteotomie ──────────────────────────────────
    "3000": ("Extraktion eines einwurzeligen Zahns", "chirurgie"),
    "3010": ("Extraktion eines mehrwurzeligen Zahns", "chirurgie"),
    "3030": ("Osteotomie einer Zahnwurzel (Freilegung/chirurg. Entfernung)", "chirurgie"),
    "3040": ("Operative Entfernung eines verlagerten/retinierten Zahns", "chirurgie"),
    "3050": ("Alveoloplastik / Glättung der Extraktionswunde (einzeitig)", "chirurgie"),
    "3060": ("Alveoloplastik mehrzeitig / Knochennivellierung", "chirurgie"),
    "3070": ("Gingivektomie / Gingivoplastik je Zahn", "chirurgie"),
    "3100": ("Inzision / Drainage eines Abszesses", "chirurgie"),
    "3110": ("Aufklappung / Lappenplastik einflächig", "chirurgie"),
    "3120": ("Aufklappung / Lappenplastik mehrflächig", "chirurgie"),
    "3130": ("Wurzelspitzenresektion (WSR)", "chirurgie"),
    "3190": ("Chirurgische Entfernung eines retinierten / verlagerten Zahns", "chirurgie"),
    "3210": ("Wundversorgung / Naht", "chirurgie"),
    "3270": ("Entfernung Nahtmaterial", "chirurgie"),

    # ── Abschnitt B – Prophylaxe (1000er) ───────────────────────────────────
    "1000": ("Mundgesundheitsaufklärung / Prophylaxegespräch", "prophylaxe"),
    "1020": ("Professionelle Zahnreinigung (PZR) je Sitzung", "prophylaxe"),
    "1040": ("Fluoridierungsmaßnahme", "prophylaxe"),

    # ── Abschnitt E – Parodontalbehandlung (4000er) ─────────────────────────
    "4000": ("Erhebung Parodontalstatus (PSI/Befund)", "parodontal"),
    "4005": ("Mundhygieneinstruktion + Remotivation", "parodontal"),
    "4010": ("Geschlossene Parodontalbehandlung (SRP) je Zahn / Sextant", "parodontal"),
    "4020": ("Medikamentöse Lokaltherapie je Zahn", "parodontal"),
    "4050": ("Parodontalchirurgie einflächig", "parodontal"),
    "4060": ("Parodontalchirurgie mehrflächig", "parodontal"),
    "4070": ("Schleim-/Bindegewebstransplantat (Rezessionsdeckung)", "parodontal"),
    "4090": ("Subgingivale Kürettage (Einzelzahn)", "parodontal"),

    # ── Abschnitt H – Aufbissbehelfe / Schienen (7000er) ────────────────────
    "7010": ("Diagnostische Aufbissschiene (einfach)", "schiene"),
    "7020": ("Therapeutische Aufbissschiene (Michigan-Schiene)", "schiene"),
    "7030": ("Stabilisierungsschiene / Knirscherschiene", "schiene"),

    # ── Abschnitt K – Implantologie (GOZ 2012, §§ 9000 ff.) ─────────────────
    #
    # KORRIGIERTE Nummern gemäß GOZ 2012:
    # 9000  Implantatinsertion (enossales Implantat)
    # 9010  Freilegung des Implantats (zweizeitiges Vorgehen)
    # 9020  Entfernung eines enossalen Implantats
    # 9030  Knochenaugmentation (GBR – gesteuerte Knochenregeneration)
    # 9040  Sinusbodenaugmentation von intraoralem Zugang (interner Sinuslift, krestal)
    # 9050  Sinusbodenaugmentation von extraoralem Zugang (externer Sinuslift, lateral)
    # 9060  Periimplantäre Therapie
    # 9070  Präprothetischer Eingriff (z.B. Vestibulumplastik)
    # 9080  Knochenentnahme und -transplantation aus extraoralem Gebiet
    #
    # ACHTUNG: "9050" wird in Charly/Solutio ZUSÄTZLICH als §6-Analog-Code
    # für "Abutment entfernen/einsetzen" verwendet (Praxisalias, nicht GOZ 2012).
    # Im Agent-Prompt wird dies als "9050a" (Analog) kenntlich gemacht.
    # ─────────────────────────────────────────────────────────────────────────
    "9000": ("Implantatinsertion – Insertion eines enossalen Implantats", "implantologie"),
    "9010": ("Freilegung des Implantats (zweizeitiges Vorgehen)", "implantologie"),
    "9020": ("Entfernung eines enossalen Implantats", "implantologie"),
    "9030": ("Knochenaugmentation – gesteuerte Knochenregeneration (GBR)", "implantologie"),
    "9040": ("Sinusbodenaugmentation interner Zugang (krestal / intralveolär)", "implantologie"),
    "9050": ("Sinusbodenaugmentation externer Zugang (lateral / Caldwell-Luc)", "implantologie"),
    "9060": ("Periimplantäre Therapie (Reinigung, Desinfektion, Defektbehandlung)", "implantologie"),
    "9070": ("Präprothetischer chirurgischer Eingriff (z.B. Vestibulumplastik)", "implantologie"),
    "9080": ("Knochenentnahme und -transplantation aus extraoralem Gebiet", "implantologie"),

    # ── §6-Analog (praxisinterne Sonderbezeichnungen in Charly) ─────────────
    # WICHTIG: "9050a" = Charly-Alias für Abutment-Management (§6-Analog).
    # Das ist NICHT identisch mit GOZ 9050 (Sinuslift extern).
    "2200i": ("§6-Analog: Implantatkrone Vollkeramik/Zirkon (analog GOZ 2210)", "analog"),
    "5120i": ("§6-Analog: Provisorische Ankerkrone auf Implantat (Langzeitprovisorium)", "analog"),
    "2270i": ("§6-Analog: Individualisiertes Abutment / Aufbauteil (analog GOZ 2270)", "analog"),
    # Charly speichert Abutment unter "9050" – GLEICHER CODE wie GOZ 9050 Sinuslift!
    # In dieser Praxis bedeutet "9050" = §6-Analog Abutment (nicht Sinuslift).
    # Deshalb erscheint 9050 sowohl in "implantologie" als auch hier als Erklärung:
    "9050_abutment": ("PRAXISALIAS: Abutment entfernen/einsetzen (Charly §6-Analog, belegt GOZ-Nr. 9050)", "analog"),
    "5190a": ("§6-Analog: Abformung mit individuellem Löffel (analog GOZ 5190)", "analog"),
    "2120z": ("§6-Analog: Stumpfaufbau Zirkon / Keramik direkt (analog GOZ 2120)", "analog"),
}

# Schnellzugriff: GOZ-Kategorien für kontextabhängige Prompt-Injektion
def goz_ref_section(kategorien: list[str]) -> str:
    """Gibt einen formatierten GOZ-Referenz-Block für den angegebenen Kategorien zurück."""
    lines = ["## GOZ-Referenz (relevante Positionen)\n"]
    for kat in kategorien:
        abschnitt = [(nr, txt) for nr, (txt, k) in GOZ_REFERENZ.items() if k == kat]
        if not abschnitt:
            continue
        kat_label = {
            "allgemein":    "Abschnitt A – Allgemeine Leistungen (0010–0099)",
            "prophylaxe":   "Abschnitt B – Prophylaxe (1000–1099)",
            "konservierend":"Abschnitt C – Konservierend / Füllungen (2000–2999)",
            "inlay":        "Abschnitt C – Inlay / Einlagefüllung (2180–2200)",
            "krone":        "Abschnitt C – Krone / Brücke (2210–2320)",
            "prothetik":    "Abschnitt F – Prothetik (5000–5999)",
            "parodontal":   "Abschnitt E – Parodontalbehandlung (4000–4099)",
            "schiene":      "Abschnitt H – Aufbissbehelfe / Schienen (7000–7099)",
            "mko":          "Abschnitt J – MKO-Paket praxisspezifisch (8000–8080, einmalig/Sitzung)",
            "chirurgie":    "Abschnitt D – Chirurgie / Extraktion (3000–3299)",
            "implantologie":"Abschnitt K – Implantologie GOZ 2012 (9000–9080)",
            "analog":       "§6-Analog – Praxisinterne Charly-Codes (nicht GOZ-Standard)",
        }.get(kat, kat)
        lines.append(f"### {kat_label}")
        for nr, txt in abschnitt:
            lines.append(f"  {nr:8s} {txt}")
        lines.append("")
    return "\n".join(lines)
