"""
HKP Multi-Agent System – GOZ-Vorschläge via Claude

Agenten:
  1. Archiv-Spezialist  → DB-Abfragen, historische Muster
  2. GOZ-Spezialist     → GOZ-Empfehlung als strukturiertes JSON (farbe: gruen/gelb/null)
  3. Qualitätsprüfung   → Review der Empfehlung auf Vollständigkeit
"""
import json
import re
import anthropic
from decimal import Decimal
from config import CLAUDE_MODEL, ANTHROPIC_API_KEY, GOZ_SESSION_EINMALIG, USE_KATALOG, goz_ref_section
import db as db_module

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _convert_decimals(obj):
    """Rekursiv Decimal → float konvertieren (für JSON-Serialisierung)."""
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimals(v) for v in obj]
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj


def _extract_json_block(text: str) -> dict | None:
    """Extrahiert JSON aus einem Text – robust gegen Trunkierung durch max_tokens."""
    import re as _re

    def _try_parse(s: str) -> dict | None:
        # 1) trailing commas entfernen  ,}  oder  ,]
        s = _re.sub(r',\s*([}\]])', r'\1', s)
        try:
            return json.loads(s)
        except Exception:
            pass
        return None

    # Versuch 1: ```json...``` Block
    match = _re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, _re.DOTALL)
    if match:
        r = _try_parse(match.group(1))
        if r is not None:
            return r

    # Versuch 2: Erstes { bis letztes }
    start = text.find('{')
    end   = text.rfind('}')
    if start >= 0 and end > start:
        r = _try_parse(text[start:end+1])
        if r is not None:
            return r

    # Versuch 3: Trunkiertes JSON reparieren
    # Finde letztes vollständiges ] in "positionen" und schließe den Wrapper
    if start >= 0:
        fragment = text[start:]
        last_bracket = fragment.rfind(']')
        if last_bracket >= 0:
            candidate = fragment[:last_bracket+1]
            # Offene String am Ende abschneiden (häufig: "begruendung": "Te)
            candidate = _re.sub(r',?\s*"[^"]*$', '', candidate)
            candidate = _re.sub(r',\s*([}\]])', r'\1', candidate)
            candidate += "]}"  # positionen-Array + Objekt schließen
            r = _try_parse(candidate)
            if r is not None:
                return r

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Tools für den Archiv-Agenten (DB-Zugriff via Claude Tool Use)
# ─────────────────────────────────────────────────────────────────────────────

ARCHIV_TOOLS = [
    {
        "name": "get_historical_goz",
        "description": (
            "Sucht die häufigsten GOZ-Positionen aus historischen Behandlungsplänen "
            "dieser Praxis für einen bestimmten Behandlungstyp (z.B. 'Keramikkrone')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "behandlung_typ": {
                    "type": "string",
                    "description": "Behandlungstyp, z.B. 'Keramikkrone', 'Kunststoffkrone', 'Verblendkrone'"
                }
            },
            "required": ["behandlung_typ"]
        }
    },
    {
        "name": "get_analog_positionen",
        "description": "Gibt §6-Analog-Positionen dieser Praxis zurück (praxisspezifische GOZ-Aliase).",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_goz_info",
        "description": "Gibt Stammdaten (Leistungstext, Praxis-Faktor) einer GOZ-Nummer zurück.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goz_nr": {"type": "string", "description": "GOZ-Nummer, z.B. '2210', '2030', '5190a'"}
            },
            "required": ["goz_nr"]
        }
    }
]


def _run_tool(tool_name: str, tool_input: dict) -> str:
    """Führt einen Tool-Call aus und gibt das Ergebnis als JSON-String zurück."""
    if tool_name == "get_historical_goz":
        result = db_module.get_historical_goz_for_treatment(tool_input["behandlung_typ"])
        return json.dumps(_convert_decimals(result), ensure_ascii=False)
    elif tool_name == "get_analog_positionen":
        result = db_module.get_praxis_analog_positionen()
        return json.dumps(_convert_decimals(result), ensure_ascii=False)
    elif tool_name == "get_goz_info":
        result = db_module.get_goz_info(tool_input["goz_nr"])
        return json.dumps(_convert_decimals(result), ensure_ascii=False)
    return json.dumps({"error": f"Unbekanntes Tool: {tool_name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Agent 1: Archiv-Spezialist
# ─────────────────────────────────────────────────────────────────────────────

def run_archiv_agent(gap_teeth: list[dict], status_callback=None) -> dict:
    """
    Agent 1: Durchsucht historische HKP-Daten dieser Praxis.
    Gibt strukturierte Zusammenfassung zurück.
    """
    treatment_types = list({t["treatment_name"] for t in gap_teeth}) if gap_teeth else []
    vollstaendigkeit_modus = bool(gap_teeth and gap_teeth[0].get("has_goz"))

    if status_callback:
        status_callback("🗄️ Archiv-Spezialist: Suche historische Muster...")

    system = """Du bist der Archiv-Spezialist einer Zahnarztpraxis.
Du analysierst historische Behandlungsdaten um typische GOZ-Kombinationen für Behandlungstypen zu ermitteln.
Nutze die verfügbaren Tools um die relevanten Daten aus der Praxis-Datenbank abzurufen.
Erstelle eine klare, strukturierte Zusammenfassung der häufigsten GOZ-Positionen.
Antworte auf Deutsch."""

    if not treatment_types:
        aufgabe = "Lade die §6-Analog-Positionen dieser Praxis und die häufigsten GOZ-Positionen allgemein."
    elif vollstaendigkeit_modus:
        aufgabe = f"Analysiere auf Vollständigkeit: {json.dumps(treatment_types, ensure_ascii=False)}\n1. Suche typische GOZ-Positionen für diese Behandlungstypen\n2. Lade §6-Analog-Positionen\n3. Erstelle Zusammenfassung für Vollständigkeitsprüfung"
    else:
        aufgabe = f"Analysiere historische GOZ-Muster für: {json.dumps(treatment_types, ensure_ascii=False)}\n1. Suche häufigste GOZ-Positionen für jeden Behandlungstyp\n2. Lade §6-Analog-Positionen\n3. Zusammenfassung: PFLICHT (>80%), STANDARD (50-80%), OPTIONAL (10-50%)"

    user_msg = f"""Analysiere bitte die historischen GOZ-Muster für folgende geplante Behandlungen:

{aufgabe}"""

    messages = [{"role": "user", "content": user_msg}]
    archiv_summary = ""

    # Agentic loop mit Tool Use
    while True:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=system,
            tools=ARCHIV_TOOLS,
            messages=messages,
        )

        if status_callback and response.stop_reason == "tool_use":
            tools_called = [b.name for b in response.content if b.type == "tool_use"]
            status_callback(f"🗄️ Archiv-Spezialist: Tool-Aufruf → {', '.join(tools_called)}")

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    archiv_summary = block.text
            break

        if response.stop_reason != "tool_use":
            break

        # Tool-Calls ausführen
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _run_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

    if status_callback:
        status_callback("✅ Archiv-Spezialist: Analyse abgeschlossen")

    return {"summary": archiv_summary, "messages": messages}


# ─────────────────────────────────────────────────────────────────────────────
# Agent 2: GOZ-Spezialist (strukturiertes JSON-Output)
# ─────────────────────────────────────────────────────────────────────────────

def _goz_agent_single_tooth(
    tooth: dict,
    existing_summary: list[str],
    archiv_summary: str,
    patient_info: dict,
    vollstaendigkeit_modus: bool = False,
    already_session_goz: set | None = None,
) -> dict:
    """
    Führt eine einzelne GOZ-Anfrage für genau einen Zahn durch.
    Gibt {"zahn": N, "behandlung": "...", "positionen": [...]} zurück.
    Durch Aufteilen auf einzelne Zähne wird Token-Overflow verhindert.

    already_session_goz: GOZ-Nummern die in dieser Sitzung bereits für einen
    früheren Zahn vorgeschlagen wurden und NICHT nochmal hinzugefügt werden sollen.
    """
    already_session_goz = already_session_goz or set()
    zahn_nr    = tooth.get("zahn", "?")
    behandlung = tooth.get("treatment_name", "")
    has_goz    = tooth.get("has_goz", False)
    is_implant = tooth.get("is_implant", False)

    # Behandlungskategorie bestimmen
    _katalog_key = tooth.get("katalog_key", "")
    _is_chirurgie    = _katalog_key in (
        "Extraktion_einwurzelig", "Extraktion_mehrwurzelig", "Extraktion_chirurgisch",
        "Implantat_Insertion", "Implantat_Augmentation", "Implantat_Freilegung",
        "WSR", "Lappenoperation",
    )
    _is_extraktion   = "Extraktion" in _katalog_key
    _is_impl_insert  = "Implantat_Insertion" == _katalog_key or "Implantat_Aug" in _katalog_key

    if vollstaendigkeit_modus or has_goz:
        aufgabe = (
            f"Prüfe ob für Zahn {zahn_nr} ({behandlung}) alle typischen GOZ-Positionen vorhanden sind. "
            "Schlage fehlende Ergänzungen vor. Vorhandene Positionen: farbe=null, fehlende Pflicht-Positionen: farbe=gruen."
        )
    elif _is_extraktion:
        _goz_basis = tooth.get("goz_basis", "3000")
        aufgabe = (
            f"Erstelle GOZ-Stückliste für Zahn {zahn_nr} – EXTRAKTION ({behandlung}). "
            f"Hauptleistung: {_goz_basis} ({'einwurzelig' if _goz_basis=='3000' else 'mehrwurzelig' if _goz_basis=='3010' else 'chirurgisch'}). "
            "Typische Begleitpositionen: 0010/0040 (Beratung/Befund), 3210 (Wundnaht), "
            "3270 (Nahtentfernung), 3050 (Alveoloplastik wenn nötig), "
            "0070 (Präventionsberatung ggf.), Anästhesieprotokoll."
        )
    elif _is_impl_insert:
        _goz_basis = tooth.get("goz_basis", "9000")
        _is_aug = "Augmentation" in _katalog_key or "9010" in _goz_basis
        aufgabe = (
            f"Erstelle GOZ-Stückliste für Zahn {zahn_nr} – IMPLANTAT-INSERTION ({behandlung}). "
            f"Hauptleistung: {_goz_basis} ({'mit Augmentation/BOD' if _is_aug else 'Standardlager'}). "
            "{'+ 9020 (Knochenersatzmaterial) PFLICHT bei Augmentation. ' if _is_aug else ''}"
            "Typische Positionen: 9000/9010 (Insertion), 9020 (Augmentation ggf.), "
            "3210 (Wundnaht), 3270 (Nahtentfernung), 0040 (Befundaufnahme), "
            "8000-8080 (MKO), 9030 (Freilegung zweizeitig ggf.). "
            "WICHTIG: Kein 9050 hier (das ist beim prothetischen Abutment-Einsetzen)."
        )
    elif _is_chirurgie:
        _goz_basis = tooth.get("goz_basis", "3130")
        aufgabe = (
            f"Erstelle GOZ-Stückliste für Zahn {zahn_nr} – CHIRURGIE ({behandlung}). "
            f"Hauptleistung: {_goz_basis}. "
            "Beachte typische Begleitpositionen: Anästhesie, Naht, Nachsorge."
        )
    elif is_implant:
        aufgabe = (
            f"Erstelle eine vollständige GOZ-Stückliste für Zahn {zahn_nr} – "
            f"KRONE AUF IMPLANTAT ({behandlung}). "
            "Hauptleistung ist 2200i (§6-Analog Implantatkrone), NICHT 2210. "
            "Zusätzlich 9050 (Abutment entfernen/wiedereinsetzen) und 2197 (Adhäsivbefestigung)."
        )
    elif "Inlay" in behandlung or "Cerec" in behandlung or "Onlay" in behandlung:
        _goz_basis = tooth.get("goz_basis", "2190")
        _flaechen  = tooth.get("karies_flaechen", "")
        _source    = tooth.get("source", "")
        _fl_hint   = f" ({_flaechen} Karies-Flächen laut Befund)" if _flaechen else ""
        _src_hint  = " (Kariesbefund aus befundze)" if _source == "karies_befundze" else ""
        aufgabe = (
            f"Erstelle eine vollständige GOZ-Stückliste für Zahn {zahn_nr} – "
            f"INLAY / CEREC-RESTAURATION{_src_hint} ({behandlung}){_fl_hint}. "
            f"Hauptleistung: {_goz_basis} "
            f"({'3-flächig' if _goz_basis=='2200' else '2-flächig' if _goz_basis=='2190' else '1-flächig'} Inlay). "
            "Optionen: 2200 (dreiflächig), 2190 (zweiflächig), 2180 (einflächig). "
            "2197 (Adhäsivbefestigung) PFLICHT. "
            "Dazu: 0040 (Befundaufnahme), 2030 (provisor. Versorgung), 5190a (Abformung), "
            "2120z (Provisorium), 8000-8080 (MKO)."
        )
    elif "Brücke" in behandlung or "Pontic" in behandlung or "Brückenglied" in behandlung:
        aufgabe = (
            f"Erstelle eine vollständige GOZ-Stückliste für Zahn {zahn_nr} – "
            f"BRÜCKENGLIED ({behandlung}). "
            "Position 2210 für das Brückenglied, dazu Ankerelemente und Abformung."
        )
    else:
        aufgabe = (
            f"Erstelle eine vollständige GOZ-Stückliste für Zahn {zahn_nr} ({behandlung})."
        )

    _is_inlay = ("Inlay" in behandlung or "Cerec" in behandlung or "Onlay" in behandlung)
    _goz_basis_tooth = tooth.get("goz_basis", "2190" if _is_inlay else "2210")

    implant_hinweis = ""
    if _is_extraktion:
        implant_hinweis = f"""
## EXTRAKTION – GOZ-Positionen
- {tooth.get('goz_basis','3000')} = Extraktion HAUPTLEISTUNG → farbe: gruen
  (3000=einwurzelig, 3010=mehrwurzelig, 3040=operative Entfernung)
- 3210 = Wundnaht (wenn genäht wird) → farbe: gelb
- 3270 = Nahtentfernung → farbe: gelb
- 3050 = Alveoloplastik (wenn erforderlich) → farbe: gelb
- 0040 = Befundaufnahme (einmalig/Sitzung) → farbe: gelb"""
    elif _is_impl_insert:
        _is_aug = "Augmentation" in _katalog_key
        implant_hinweis = f"""
## IMPLANTAT-INSERTION – GOZ-Positionen
- {tooth.get('goz_basis','9000')} = Implantat-Insertion HAUPTLEISTUNG → farbe: gruen
  (9000=Standardlager, 9010=augmentiertes Lager)
{'- 9020 = Knochenersatzmaterial/Augmentation → farbe: gruen' if _is_aug else '- 9020 = Augmentation (falls erforderlich) → farbe: gelb'}
- 3210 = Wundnaht → farbe: gelb
- 3270 = Nahtentfernung → farbe: gelb
- 0040 = Befundaufnahme → farbe: gelb
- 8000-8080 = MKO-Paket → farbe: gruen
- 9030 = Freilegung (nur bei zweizeitig) → farbe: gelb
NICHT verwenden: 9050 (das ist beim Abutment-Einsetzen bei der Prothetik)"""
    elif is_implant:
        implant_hinweis = """
## IMPLANTAT-KRONE – besondere GOZ-Positionen
- 2200i = §6 Analog Implantatkrone (HAUPTLEISTUNG statt 2210) → farbe: gruen
- 9050  = Entfernen und Wiedereinsetzen des Sekundärteils (Abutment) → farbe: gruen
- 2197  = Adhäsive Befestigung → farbe: gruen
- 5120i = Provisorische Ankerkrone auf Implantat → farbe: gelb
- 5190a = Abformung individ. Löffel → farbe: gelb
- MKO-Paket 8000-8080 → farbe: gruen"""
    elif _is_inlay:
        implant_hinweis = f"""
## INLAY / CEREC – besondere GOZ-Positionen
- {_goz_basis_tooth} = Keramik-Inlay (HAUPTLEISTUNG) → farbe: gruen
  (2180=einflächig, 2190=zweiflächig, 2200=dreiflächig)
- 2197 = Adhäsive Befestigung (PFLICHT bei Vollkeramik/Cerec) → farbe: gruen
- 0040 = Befundaufnahme → farbe: gelb
- 2030 = Provisorische Versorgung → farbe: gelb
- 5190a = Abformung individ. Löffel → farbe: gelb
- 2120z = Provisorisches Inlay/Aufbau → farbe: gelb
- MKO-Paket 8000-8080 → farbe: gruen"""

    # GOZ-Referenz je nach Behandlungstyp zusammenstellen
    if _is_chirurgie or _is_extraktion or _is_impl_insert:
        _goz_kategorien = ["allgemein", "chirurgie", "implantologie", "mko"]
    elif is_implant:
        _goz_kategorien = ["allgemein", "krone", "prothetik", "implantologie", "analog", "mko"]
    elif _is_inlay:
        _goz_kategorien = ["allgemein", "konservierend", "inlay", "prothetik", "mko"]
    else:
        _goz_kategorien = ["allgemein", "konservierend", "krone", "prothetik", "analog", "mko"]

    _goz_ref_block = goz_ref_section(_goz_kategorien)

    system = (
        "Du bist ein spezialisierter GOZ-Abrechnungsexperte für Zahnärzte in Deutschland. "
        "Du kennst die GOZ 2012 vollständig – prothetische, chirurgische und implantologische Positionen. "
        "Verwende ausschließlich die unten aufgeführten GOZ-Positionen. "
        "Antworte AUSSCHLIESSLICH mit validem JSON, ohne Text davor oder danach.\n\n"
        + _goz_ref_block
    )

    _default_goz_nr = "2200i" if is_implant else (_goz_basis_tooth if _is_inlay else "2210")
    _default_goz_txt = ("Implantatkrone §6-Analog" if is_implant
                        else (f"Keramik-Inlay {_goz_basis_tooth}" if _is_inlay
                              else "Vollkeramikkrone"))

    # ── Stücklisten-Katalog (wenn aktiviert und vorhanden) ───────────────
    _katalog_section = ""
    if USE_KATALOG and not vollstaendigkeit_modus:
        try:
            from katalog_builder import get_template_for_tooth, template_to_prompt_str
            _kat_key, _kat_pos = get_template_for_tooth(tooth)
            if _kat_pos:
                _katalog_section = "\n" + template_to_prompt_str(_kat_pos) + "\n"
        except Exception:
            pass   # Katalog nicht verfügbar → silent fallback

    # Hinweis auf bereits vorgeschlagene Session-GOZ
    _session_already = sorted(already_session_goz & GOZ_SESSION_EINMALIG)
    _mko_remaining   = [n for n in ["8000","8010","8020","8060","8080"] if n not in already_session_goz]
    _session_hint = ""
    if _session_already:
        _session_hint = (
            f"\n\n## ⚠️ SESSION-GOZ BEREITS VORGESCHLAGEN (NICHT nochmal hinzufügen!)\n"
            f"Folgende Positionen sind Sitzungs-GOZ und wurden bereits für einen anderen Zahn "
            f"vorgeschlagen. Sie dürfen in dieser Liste NICHT nochmal erscheinen:\n"
            + "".join(f"- **{n}** (bereits im Paket)\n" for n in _session_already)
        )
        if not _mko_remaining:
            _session_hint += "→ MKO-Paket KOMPLETT bereits vorgeschlagen – weglassen!\n"

    user_msg = f"""## Patientenkontext
Patient: {patient_info.get('name', '')} {patient_info.get('vorname', '')}
KV-Bezeichnung: {patient_info.get('kurztext', '')}

## Vorhandene GOZ-Einträge im KV (Referenz)
{chr(10).join(existing_summary[:30]) if existing_summary else "  (keine)"}
{_katalog_section}
## Praxis-Archiv-Muster
{archiv_summary[:600]}
{implant_hinweis}{_session_hint}
## Aufgabe
{aufgabe}

Berücksichtige:
1. MKO-Paket (8000, 8010, 8020, 8060, 8080) = nur EINMAL pro Sitzung, {f"bereits vorgeschlagen – WEGLASSEN" if not _mko_remaining else f"noch benötigt: {', '.join(_mko_remaining)}"}
2. Hauptleistung: {"2200i (Implantatkrone §6-Analog)" if is_implant else (f"{_goz_basis_tooth} (Inlay)" if _is_inlay else "z.B. 2210 Keramikkrone")}
3. Praxisstandards: 2030, 5190a, 2270, 2120z, Ä1, Ä5, 5110a, 2197
4. §6-Analog-Positionen aus dem Archiv
5. Faktor: Praxisüblich 3,5; Regelfall 2,3

Farbregeln "farbe":
- "gruen" = PFLICHT (Hauptleistung, MKO wenn noch nicht vorgeschlagen, 2197 bei Vollkeramik{", 9050+2200i bei Implantat" if is_implant else ""})
- "gelb" = EMPFOHLEN (Praxisstandard)
- null = OPTIONAL

Antworte NUR mit diesem JSON (kein Wrapper, kein "zaehne"-Array):
{{
  "zahn": {zahn_nr},
  "behandlung": "{behandlung}",
  "positionen": [
    {{
      "goz_nr": "{_default_goz_nr}",
      "text": "{_default_goz_txt}",
      "faktor": 3.5,
      "anzahl": 1,
      "farbe": "gruen",
      "begruendung": "Hauptleistung"
    }}
  ]
}}"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            raw_text = block.text

    # stop_reason prüfen – bei max_tokens wird JSON trunkiert
    stop_reason = getattr(response, "stop_reason", "end_turn")

    parsed = _extract_json_block(raw_text)
    if parsed and "positionen" in parsed:
        if stop_reason == "max_tokens":
            parsed["_truncated"] = True   # für Debugging sichtbar
        return parsed

    # Fallback
    return {
        "zahn": zahn_nr,
        "behandlung": behandlung,
        "positionen": [],
        "_parse_error": True,
        "_raw": raw_text[:500],
    }


def run_goz_agent(
    gap_teeth: list[dict],
    existing_goz: list[dict],
    archiv_summary: str,
    patient_info: dict,
    status_callback=None,
) -> dict:
    """
    Agent 2: Generiert GOZ-Vorschläge – pro Zahn eine separate API-Anfrage.
    So wird Token-Overflow bei vielen Zähnen sicher verhindert.
    """
    if not gap_teeth:
        return {"zaehne": [], "gesamtbegruendung": "Keine Zähne zur Analyse."}

    vollstaendigkeit_modus = bool(gap_teeth[0].get("has_goz"))

    # Kompakte Zusammenfassung der vorhandenen GOZ für den Kontext
    existing_summary = []
    for p in existing_goz:
        if p.get("goz_nr"):
            zaehne = db_module.bitmask_to_fdi(p.get("zahn_bitmask") or 0)
            existing_summary.append(
                f"  Zahn {zaehne}: {p['goz_nr']} – {(p.get('goz_text') or '')[:50]} ×{p.get('faktor','-')}"
            )

    zaehne_results = []
    total = len(gap_teeth)
    # Session-GOZ Tracking: verhindert MKO-Doppelungen über mehrere Zähne
    session_goz_proposed: set[str] = set()

    for i, tooth in enumerate(gap_teeth):
        zahn_nr = tooth.get("zahn", "?")
        if status_callback:
            status_callback(
                f"⚕️ GOZ-Spezialist: Zahn {zahn_nr} ({i+1}/{total})..."
            )
        result = _goz_agent_single_tooth(
            tooth=tooth,
            existing_summary=existing_summary,
            archiv_summary=archiv_summary,
            patient_info=patient_info,
            vollstaendigkeit_modus=vollstaendigkeit_modus,
            already_session_goz=session_goz_proposed.copy(),
        )
        # Nach Analyse: vorgeschlagene Session-GOZ merken
        for pos in result.get("positionen", []):
            nr = pos.get("goz_nr", "")
            if nr in GOZ_SESSION_EINMALIG:
                session_goz_proposed.add(nr)
        zaehne_results.append(result)

    any_parse_error = any(z.get("_parse_error") for z in zaehne_results)

    if status_callback:
        status_callback("✅ GOZ-Spezialist: Alle Zähne analysiert")

    return {
        "zaehne": zaehne_results,
        "gesamtbegruendung": (
            f"{total} Zahn/Zähne analysiert. "
            + ("⚠️ Einige Zähne konnten nicht vollständig geparst werden." if any_parse_error else "")
        ),
        "_parse_error": any_parse_error,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Agent 3: Qualitätsprüfung
# ─────────────────────────────────────────────────────────────────────────────

def run_quality_check(
    goz_structured: dict,
    gap_teeth: list[dict],
    status_callback=None,
) -> str:
    """
    Agent 3: Prüft die GOZ-Vorschläge auf Vollständigkeit und Konsistenz.
    """
    if status_callback:
        status_callback("🔍 Qualitätsprüfung: Überprüfe Vollständigkeit...")

    system = """Du bist der Qualitätsprüfer für GOZ-Abrechnungen in einer Zahnarztpraxis.
Du prüfst ob GOZ-Vorschläge vollständig, konsistent und abrechnungskonform sind.
Du kennst häufige Fehler und Auslassungen bei der GOZ-Abrechnung.
Antworte auf Deutsch, kritisch aber konstruktiv."""

    user_msg = f"""Prüfe folgende GOZ-Vorschläge auf Vollständigkeit:

## Zu behandelnde Zähne
{json.dumps(gap_teeth, ensure_ascii=False, indent=2)}

## GOZ-Vorschläge des Spezialisten
{json.dumps(goz_structured, ensure_ascii=False, indent=2)}

## Prüfkriterien
1. ✅ Sind alle Pflicht-Positionen enthalten? (Hauptleistung, MKO-Paket 8000-8080 falls nicht im KV)
2. ✅ Sind Steigerungsfaktoren >2,3 begründbar?
3. ✅ Fehlen typische Standard-Positionen (5190a, 2270, 2120z, 2030)?
4. ✅ Ist 2197 (Adhäsivbefestigung) bei Vollkeramik enthalten?
5. ✅ Gibt es Widersprüche oder Ausschlüsse?

Formatiere die Ausgabe:
### ✅ Bestätigt
[was korrekt ist]

### ⚠️ Hinweise
[was zu beachten ist]

### ❌ Fehlend / Korrekturbedarf
[was ergänzt/korrigiert werden sollte]

### 📋 Finale Empfehlung
[kurze zusammenfassende Empfehlung]"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    result = ""
    for block in response.content:
        if hasattr(block, "text"):
            result = block.text

    if status_callback:
        status_callback("✅ Qualitätsprüfung: Abgeschlossen")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrierung: Alle 3 Agenten sequenziell
# ─────────────────────────────────────────────────────────────────────────────

def run_hkp_pipeline(
    gap_teeth: list[dict],
    existing_goz: list[dict],
    patient_info: dict,
    status_callback=None,
) -> dict:
    """
    Führt die gesamte HKP-Agent-Pipeline aus:
    Archiv → GOZ-Spezialist → Qualitätsprüfung

    Returns:
        {
          "archiv": str,          # Markdown-Zusammenfassung Archiv-Analyse
          "goz_structured": dict, # Strukturiertes JSON mit farbe-Feldern
          "qualitaet": str,       # Markdown Qualitätsprüfung
        }
    """
    # Agent 1: Archiv
    archiv_result = run_archiv_agent(gap_teeth, status_callback=status_callback)
    archiv_summary = archiv_result["summary"]

    # Agent 2: GOZ-Spezialist (gibt jetzt strukturiertes JSON zurück)
    goz_structured = run_goz_agent(
        gap_teeth=gap_teeth,
        existing_goz=existing_goz,
        archiv_summary=archiv_summary,
        patient_info=patient_info,
        status_callback=status_callback,
    )

    # Agent 3: Qualitätsprüfung
    qualitaet = run_quality_check(
        goz_structured=goz_structured,
        gap_teeth=gap_teeth,
        status_callback=status_callback,
    )

    return {
        "archiv": archiv_summary,
        "goz_structured": goz_structured,
        "qualitaet": qualitaet,
    }
