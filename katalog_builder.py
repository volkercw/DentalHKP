"""
Stücklisten-Katalog – Praxisbasierte GOZ-Schablonen

Workflow:
  1. DB-Analyse: Historische KV-Muster je Behandlungstyp
  2. Chefarzt-Agent: KI annotiert & strukturiert die rohen Frequenzdaten
  3. Katalog-JSON: Persistente Schablone in KATALOG_PFAD
  4. Pipeline-Integration: hkp_agents nutzt Schablone als primäre Basis

Katalogschlüssel (behandlung_typ):
  Keramikkrone | Keramikkrone_Implantat
  Inlay_Cerec  (mit Varianten: 1-flächig / 2-flächig / 3-flächig+)
  Verblendkrone | Metallkrone | Kunststoffkrone
  Teleskopkrone | Brückenglied_VK
"""

import json
import datetime
import anthropic
from pathlib import Path

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, PROJEKTE_PFAD,
    GOZ_SESSION_EINMALIG,
)
import db as db_module

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

KATALOG_DATEI = Path(PROJEKTE_PFAD) / "katalog_stuecklisten.json"

# ─────────────────────────────────────────────────────────────────────────────
# Behandlungsdefinitionen – welche DB-Muster gehören zu welchem Katalogtyp
# ─────────────────────────────────────────────────────────────────────────────

BEHANDLUNGEN_CONFIG: dict[str, dict] = {
    "Keramikkrone": {
        "bezeichnung":    "Keramikkrone (Vollkeramik)",
        "befund_codes":   ["08"],
        "kv_patterns":    ["%Keramik%Vollkrone%", "%Vollkeramik%", "%Keramik%Krone%"],
        "implant":        False,
        "varianten":      None,
        "haupt_goz":      "2210",
    },
    "Keramikkrone_Implantat": {
        "bezeichnung":    "Keramikkrone auf Implantat (§6-Analog)",
        "befund_codes":   ["08"],
        "kv_patterns":    ["%Implantat%Krone%", "%Implantatkrone%", "%Implantat%"],
        "implant":        True,
        "varianten":      None,
        "haupt_goz":      "2200i",
    },
    "Verblendkrone": {
        "bezeichnung":    "Verblendkrone (VMK / Metall-Keramik)",
        "befund_codes":   ["05"],
        "kv_patterns":    ["%Verblend%", "%VMK%", "%Metall%Keramik%"],
        "implant":        False,
        "varianten":      None,
        "haupt_goz":      "2210",
    },
    "Metallkrone": {
        "bezeichnung":    "Metallkrone (Vollguss)",
        "befund_codes":   ["02"],
        "kv_patterns":    ["%Metall%Vollkrone%", "%Vollguss%"],
        "implant":        False,
        "varianten":      None,
        "haupt_goz":      "2210",
    },
    "Teleskopkrone": {
        "bezeichnung":    "Teleskopkrone / Konuskrone",
        "befund_codes":   ["88", "8e"],
        "kv_patterns":    ["%Teleskop%", "%Konus%"],
        "implant":        False,
        "varianten":      None,
        "haupt_goz":      "2210",
    },
    "Inlay_Cerec": {
        "bezeichnung":    "Inlay / Cerec-Restauration (Keramik)",
        "befund_codes":   ["0\xc0"],
        "kv_patterns":    ["%Inlay%", "%Cerec%", "%Onlay%"],
        "implant":        False,
        "varianten": {
            "1-flächig":   {"haupt_goz": "2180", "trigger_goz": ["2180"]},
            "2-flächig":   {"haupt_goz": "2190", "trigger_goz": ["2190"]},
            "3-flächig+":  {"haupt_goz": "2200", "trigger_goz": ["2200"]},
        },
        "haupt_goz":      "2190",   # Default-Variante
    },
    "Brückenglied_VK": {
        "bezeichnung":    "Brückenglied Vollkeramik (Pontic)",
        "befund_codes":   ["0\xba"],
        "kv_patterns":    ["%Brücken%", "%Pontic%", "%Brücke%"],
        "implant":        False,
        "varianten":      None,
        "haupt_goz":      "2210",
    },
}


def find_katalog_key(treatment_name: str, is_implant: bool = False,
                     goz_basis: str = None) -> str | None:
    """Ermittelt den Katalogschlüssel für einen Behandlungseintrag aus gap_teeth."""
    tn = treatment_name.lower()

    if is_implant or (goz_basis and "i" in str(goz_basis)):
        return "Keramikkrone_Implantat"

    if "inlay" in tn or "cerec" in tn or "onlay" in tn or "karies" in tn:
        return "Inlay_Cerec"
    if "vollkeramik" in tn or "keramikkrone" in tn or "keramik" in tn:
        return "Keramikkrone"
    if "verblend" in tn or "vmk" in tn or "metall-keramik" in tn:
        return "Verblendkrone"
    if "metallkrone" in tn or "vollguss" in tn:
        return "Metallkrone"
    if "teleskop" in tn or "konus" in tn:
        return "Teleskopkrone"
    if "brückenglied" in tn or "pontic" in tn:
        return "Brückenglied_VK"
    return None


def get_inlay_variante(karies_flaechen: int | None, goz_basis: str | None) -> str:
    """Bestimmt die Inlay-Variante aus Flächenanzahl oder GOZ-Basis."""
    if goz_basis == "2180":
        return "1-flächig"
    if goz_basis == "2190":
        return "2-flächig"
    if goz_basis == "2200":
        return "3-flächig+"
    if karies_flaechen:
        if karies_flaechen <= 1: return "1-flächig"
        if karies_flaechen == 2: return "2-flächig"
        return "3-flächig+"
    return "2-flächig"   # Defaultfall


# ─────────────────────────────────────────────────────────────────────────────
# DB-Analyse: Rohdaten je Behandlungstyp
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_raw_patterns(behandlung_key: str, limit_kvs: int = 500) -> tuple[list[dict], int]:
    """
    Holt GOZ-Häufigkeitsdaten aus historischen KVs für einen Behandlungstyp.
    Für Inlay-Varianten wird nach dem Haupt-GOZ (2180/2190/2200) gesucht.

    Returns: (pattern_list, n_kvs_analysiert)
    """
    cfg = BEHANDLUNGEN_CONFIG[behandlung_key]
    patterns = cfg["kv_patterns"]
    varianten = cfg.get("varianten")

    all_rows: list[dict] = []
    kv_ids: set = set()

    import psycopg2
    conn_cfg = db_module.DB_CONFIG if hasattr(db_module, "DB_CONFIG") else None

    with db_module.get_connection() as conn:
        with conn.cursor() as cur:

            if varianten:
                # Inlay-Varianten: erst Basis-Muster, dann je Variante aufteilen
                for variant_name, vd in varianten.items():
                    for trigger in vd["trigger_goz"]:
                        cur.execute(
                            """SELECT DISTINCT km.kvid
                               FROM public.kv_daten kd
                               JOIN public.kv_main km ON km.solid = kd.kvmainid
                               WHERE kd.nummer = %s
                               ORDER BY km.kvid DESC LIMIT %s""",
                            (trigger, limit_kvs // len(varianten))
                        )
                        for row in cur.fetchall():
                            kv_ids.add(row[0])
            else:
                # Standard: kv_main.bezeichnung ILIKE pattern
                for pat in patterns:
                    cur.execute(
                        """SELECT DISTINCT kvid FROM public.kv_main
                           WHERE bezeichnung ILIKE %s
                           ORDER BY kvid DESC LIMIT %s""",
                        (pat, limit_kvs)
                    )
                    for row in cur.fetchall():
                        kv_ids.add(row[0])

            if not kv_ids:
                return [], 0

            n_kvs = len(kv_ids)
            kv_list = list(kv_ids)

            # GOZ-Frequenzen über alle gesammelten KVs
            placeholders = ",".join(["%s"] * len(kv_list))
            cur.execute(
                f"""SELECT
                        kd.nummer                             AS goz_nr,
                        MAX(kd.bezeichnung)                   AS text,
                        COUNT(DISTINCT km.kvid)               AS kvs_mit_position,
                        COUNT(*)                              AS gesamt_vorkommen,
                        ROUND(AVG(kd.mp::numeric), 2)         AS avg_faktor,
                        ROUND(AVG(kd.betrag::numeric), 2)     AS avg_betrag,
                        ROUND(AVG(kd.anzahl::numeric), 2)     AS avg_anzahl
                    FROM public.kv_daten kd
                    JOIN public.kv_main km ON km.solid = kd.kvmainid
                    WHERE km.kvid IN ({placeholders})
                      AND kd.nummer IS NOT NULL AND kd.nummer != ''
                    GROUP BY kd.nummer
                    ORDER BY kvs_mit_position DESC
                    LIMIT 50""",
                kv_list
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            # Relative Häufigkeit berechnen
            for r in rows:
                r["haeufigkeit_pct"] = round(
                    (int(r["kvs_mit_position"]) / n_kvs) * 100, 1
                )
                # Numerische Typen
                for k in ("avg_faktor", "avg_betrag", "avg_anzahl"):
                    r[k] = float(r[k] or 0)

            return rows, n_kvs


def _fetch_raw_patterns_inlay_variante(variante_key: str, trigger_goz: str,
                                       limit_kvs: int = 300) -> tuple[list[dict], int]:
    """Rohdaten speziell für eine Inlay-Variante (2180/2190/2200 als Ankerpunkt)."""
    with db_module.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT km.kvid
                   FROM public.kv_daten kd
                   JOIN public.kv_main km ON km.solid = kd.kvmainid
                   WHERE kd.nummer = %s
                   ORDER BY km.kvid DESC LIMIT %s""",
                (trigger_goz, limit_kvs)
            )
            kv_ids = [r[0] for r in cur.fetchall()]
            if not kv_ids:
                return [], 0

            n_kvs = len(kv_ids)
            placeholders = ",".join(["%s"] * len(kv_ids))
            cur.execute(
                f"""SELECT
                        kd.nummer                            AS goz_nr,
                        MAX(kd.bezeichnung)                  AS text,
                        COUNT(DISTINCT km.kvid)              AS kvs_mit_position,
                        ROUND(AVG(kd.mp::numeric), 2)        AS avg_faktor,
                        ROUND(AVG(kd.anzahl::numeric), 2)    AS avg_anzahl
                    FROM public.kv_daten kd
                    JOIN public.kv_main km ON km.solid = kd.kvmainid
                    WHERE km.kvid IN ({placeholders})
                      AND kd.nummer IS NOT NULL AND kd.nummer != ''
                    GROUP BY kd.nummer
                    ORDER BY kvs_mit_position DESC LIMIT 40""",
                kv_ids
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                r["haeufigkeit_pct"] = round(int(r["kvs_mit_position"]) / n_kvs * 100, 1)
                r["avg_faktor"]  = float(r["avg_faktor"]  or 0)
                r["avg_anzahl"]  = float(r["avg_anzahl"]  or 0)
            return rows, n_kvs


# ─────────────────────────────────────────────────────────────────────────────
# Chefarzt-Agent: annotiert Rohdaten → strukturierte Stückliste
# ─────────────────────────────────────────────────────────────────────────────

def _run_chefarzt_agent(
    behandlung_key: str,
    behandlung_bezeichnung: str,
    haupt_goz: str,
    raw_patterns: list[dict],
    n_kvs: int,
    variante_name: str | None = None,
    status_callback=None,
) -> dict:
    """
    Chefarzt-Agent analysiert Rohdaten und erstellt eine annotierte Stückliste.
    Returns dict mit "positionen" und "chefarzt_hinweise".
    """
    if status_callback:
        v = f" [{variante_name}]" if variante_name else ""
        status_callback(f"👨‍⚕️ Chefarzt: Analysiere {behandlung_bezeichnung}{v}...")

    # Nur Top-25 Positionen übergeben, kompakt ohne Einrückung → weniger Tokens
    raw_compact = [
        {"goz_nr": r["goz_nr"], "text": r["text"][:50],
         "pct": r["haeufigkeit_pct"], "faktor": r["avg_faktor"]}
        for r in raw_patterns[:25]
    ]
    raw_json = json.dumps(raw_compact, ensure_ascii=False)

    variante_info = f" (Variante: {variante_name})" if variante_name else ""

    # Session-GOZ als kompakte Liste
    mko_nrs = "8000,8010,8020,8030,8040,8050,8060,8070,8080"

    system = (
        "Du bist leitender Zahnarzt und GOZ-Abrechnungsexperte. "
        "Antworte AUSSCHLIESSLICH mit validem, vollständigem JSON – kein Text davor oder danach."
    )

    # Beispielposition als Anker für exaktes Format
    beispiel = (
        f'{{"goz_nr":"{haupt_goz}","text":"Hauptleistung",'
        f'"kategorie":"pflicht","session_einmalig":false,'
        f'"avg_faktor":3.5,"avg_anzahl":1.0,"haeufigkeit_pct":95.0,'
        f'"reihenfolge":1,"begruendung":"Kernleistung"}}'
    )

    user_msg = f"""Erstelle GOZ-Stückliste für: {behandlung_bezeichnung}{variante_info}
Basis: {n_kvs} historische KV-Fälle dieser Praxis.
Hauptleistung: GOZ {haupt_goz}

FREQUENZDATEN (goz_nr,text,pct=Häufigkeit%,faktor):
{raw_json}

REGELN:
- kategorie "pflicht": ≥80% ODER medizinisch zwingend
- kategorie "empfohlen": 50-79%
- kategorie "optional": <50%
- kategorie "session": MKO-Paket ({mko_nrs}) → session_einmalig:true, nur 1× pro Sitzung
- Max 15 Positionen, nur klinisch relevante
- GOZ {haupt_goz} = reihenfolge:1, kategorie:"pflicht"
- avg_faktor aus Frequenzdaten übernehmen, mind. 2.3

AUSGABE – exakt dieses JSON-Format (keine anderen Felder!):
{{"positionen":[{beispiel},...],
"chefarzt_hinweise":"kurzer Hinweis max 150 Zeichen",
"qualitaet_score":85}}"""

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

    # Robuste JSON-Extraktion (auch bei truncation / trailing commas / Erklärtext)
    parsed = _robust_json_extract(raw_text)

    if not parsed or "positionen" not in parsed:
        return {
            "positionen": [],
            "chefarzt_hinweise": f"⚠️ Parse-Fehler (stop={response.stop_reason}, "
                                 f"tokens={response.usage.output_tokens})",
            "qualitaet_score": 0,
            "_parse_error": True,
            "_raw": raw_text[:400],
        }

    if status_callback:
        v = f" [{variante_name}]" if variante_name else ""
        n_pos = len(parsed.get("positionen", []))
        status_callback(f"✅ Chefarzt: {behandlung_bezeichnung}{v} → {n_pos} Positionen")

    return parsed


def _robust_json_extract(text: str) -> dict | None:
    """
    Robuste JSON-Extraktion aus LLM-Antworten.
    Behandelt: Code-Blöcke, Erklärtext vor/nach JSON, trailing commas,
    abgeschnittene Responses (max_tokens).
    """
    import re as _re

    def _try_parse(s: str) -> dict | None:
        # Trailing commas vor } und ] entfernen
        s = _re.sub(r',\s*([}\]])', r'\1', s)
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    # 1) Code-Block ```json...```
    m = _re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if m:
        result = _try_parse(m.group(1))
        if result:
            return result

    # 2) Direkt: erstes { bis letztes } im Text
    start = text.find('{')
    end   = text.rfind('}')
    if start >= 0 and end > start:
        result = _try_parse(text[start:end + 1])
        if result:
            return result

    # 3) Abgeschnittene Response: letztes vollständiges ] schließen und } anhängen
    if start >= 0:
        # Finde das letzte vollständige ] um die positionen-Liste zu retten
        last_bracket = text.rfind(']')
        if last_bracket > start:
            truncated = text[start:last_bracket + 1] + ',"chefarzt_hinweise":"(gekürzt)","qualitaet_score":50}}'
            # Manchmal fehlt das innere Objekt-} – versuche es trotzdem
            result = _try_parse(truncated)
            if result and "positionen" in result:
                return result

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Katalog aufbauen
# ─────────────────────────────────────────────────────────────────────────────

def build_katalog(
    behandlung_keys: list[str] | None = None,
    status_callback=None,
) -> dict:
    """
    Hauptfunktion: Baut den kompletten Stücklisten-Katalog aus der DB auf.
    Nutzt den Chefarzt-Agenten für jede Behandlungsart.

    behandlung_keys: Liste der zu analysierenden Behandlungstypen.
                     None = alle.
    """
    keys = behandlung_keys or list(BEHANDLUNGEN_CONFIG.keys())
    total = len(keys)
    katalog: dict = {
        "schema_version": "1.0",
        "erstellt_am": datetime.datetime.now().isoformat(),
        "kvs_analysiert_gesamt": 0,
        "behandlungen": {},
    }

    if status_callback:
        status_callback(f"📚 Stücklisten-Katalog: Starte Analyse für {total} Behandlungstypen...")

    for idx, key in enumerate(keys):
        cfg = BEHANDLUNGEN_CONFIG[key]
        bezeichnung = cfg["bezeichnung"]
        haupt_goz   = cfg["haupt_goz"]
        varianten   = cfg.get("varianten")

        if status_callback:
            status_callback(f"🔍 DB-Analyse ({idx+1}/{total}): {bezeichnung}...")

        if varianten:
            # ── Inlay / Cerec: Basis + je Variante ──────────────────────────
            # 1. Gemeinsame Basis (alle Inlay-KVs)
            raw_basis, n_basis = _fetch_raw_patterns(key, limit_kvs=400)
            katalog["kvs_analysiert_gesamt"] += n_basis

            # 2. Je Variante separat analysieren
            varianten_out: dict = {}
            for var_name, vd in varianten.items():
                trigger = vd["trigger_goz"][0]
                raw_var, n_var = _fetch_raw_patterns_inlay_variante(var_name, trigger)

                if n_var > 10:   # Nur wenn genug Daten
                    annotiert = _run_chefarzt_agent(
                        behandlung_key=key,
                        behandlung_bezeichnung=bezeichnung,
                        haupt_goz=vd["haupt_goz"],
                        raw_patterns=raw_var,
                        n_kvs=n_var,
                        variante_name=var_name,
                        status_callback=status_callback,
                    )
                else:
                    # Zu wenig Daten → Fallback auf Basismuster
                    annotiert = _run_chefarzt_agent(
                        behandlung_key=key,
                        behandlung_bezeichnung=bezeichnung,
                        haupt_goz=vd["haupt_goz"],
                        raw_patterns=raw_basis,
                        n_kvs=n_basis,
                        variante_name=var_name,
                        status_callback=status_callback,
                    )
                varianten_out[var_name] = {
                    "haupt_goz":      vd["haupt_goz"],
                    "kvs_analysiert": n_var,
                    "positionen":     annotiert.get("positionen", []),
                    "chefarzt_hinweise": annotiert.get("chefarzt_hinweise", ""),
                }

            katalog["behandlungen"][key] = {
                "bezeichnung":      bezeichnung,
                "codes":            cfg["befund_codes"],
                "implant":          cfg["implant"],
                "haupt_goz":        haupt_goz,
                "hat_varianten":    True,
                "varianten":        varianten_out,
                "kvs_analysiert":   n_basis,
                "erstellt_am":      datetime.datetime.now().isoformat(),
            }

        else:
            # ── Standard-Behandlung (1 Stückliste) ──────────────────────────
            raw, n_kvs = _fetch_raw_patterns(key)
            katalog["kvs_analysiert_gesamt"] += n_kvs

            if n_kvs >= 5:
                annotiert = _run_chefarzt_agent(
                    behandlung_key=key,
                    behandlung_bezeichnung=bezeichnung,
                    haupt_goz=haupt_goz,
                    raw_patterns=raw,
                    n_kvs=n_kvs,
                    status_callback=status_callback,
                )
            else:
                annotiert = {"positionen": [], "chefarzt_hinweise": "Zu wenig Daten.", "qualitaet_score": 0}

            katalog["behandlungen"][key] = {
                "bezeichnung":      bezeichnung,
                "codes":            cfg["befund_codes"],
                "implant":          cfg["implant"],
                "haupt_goz":        haupt_goz,
                "hat_varianten":    False,
                "positionen":       annotiert.get("positionen", []),
                "chefarzt_hinweise": annotiert.get("chefarzt_hinweise", ""),
                "qualitaet_score":  annotiert.get("qualitaet_score", 0),
                "kvs_analysiert":   n_kvs,
                "erstellt_am":      datetime.datetime.now().isoformat(),
            }

    if status_callback:
        n_b = len(katalog["behandlungen"])
        status_callback(f"✅ Stücklisten-Katalog fertig: {n_b} Behandlungstypen, "
                        f"{katalog['kvs_analysiert_gesamt']} KVs analysiert.")
    return katalog


# ─────────────────────────────────────────────────────────────────────────────
# Katalog speichern / laden
# ─────────────────────────────────────────────────────────────────────────────

def save_katalog(katalog: dict) -> Path:
    """Speichert Katalog als JSON-Datei."""
    KATALOG_DATEI.parent.mkdir(parents=True, exist_ok=True)
    with open(KATALOG_DATEI, "w", encoding="utf-8") as f:
        json.dump(katalog, f, ensure_ascii=False, indent=2)
    return KATALOG_DATEI


def load_katalog() -> dict | None:
    """Lädt Katalog aus JSON-Datei. None wenn nicht vorhanden."""
    if not KATALOG_DATEI.exists():
        return None
    try:
        with open(KATALOG_DATEI, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def katalog_info() -> dict:
    """Gibt Metadaten des gespeicherten Katalogs zurück."""
    k = load_katalog()
    if not k:
        return {"vorhanden": False}
    behandlungen = k.get("behandlungen", {})
    return {
        "vorhanden":              True,
        "erstellt_am":            k.get("erstellt_am", "?"),
        "kvs_gesamt":             k.get("kvs_analysiert_gesamt", 0),
        "n_behandlungstypen":     len(behandlungen),
        "behandlungstypen":       list(behandlungen.keys()),
        "qualitaet_scores":       {
            k2: v.get("qualitaet_score", 0)
            for k2, v in behandlungen.items()
            if not v.get("hat_varianten")
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Template-Lookup für den Agent-Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def get_template(
    behandlung_key: str,
    variante: str | None = None,
    katalog: dict | None = None,
) -> list[dict] | None:
    """
    Gibt die Stückliste (Positionen-Liste) für einen Behandlungstyp zurück.
    Bei Inlay-Varianten: variante z.B. "2-flächig".
    Gibt None zurück wenn kein Katalog vorhanden.
    """
    k = katalog or load_katalog()
    if not k:
        return None

    eintrag = k.get("behandlungen", {}).get(behandlung_key)
    if not eintrag:
        return None

    if eintrag.get("hat_varianten"):
        # Variante wählen (oder erste verfügbare)
        varianten = eintrag.get("varianten", {})
        var_eintrag = varianten.get(variante) or next(iter(varianten.values()), None)
        if not var_eintrag:
            return None
        return var_eintrag.get("positionen", [])
    else:
        return eintrag.get("positionen", [])


def get_template_for_tooth(
    tooth: dict,
    katalog: dict | None = None,
) -> tuple[str | None, list[dict]]:
    """
    Convenience-Wrapper: ermittelt Katalogschlüssel + Variante aus einem
    gap_teeth-Eintrag und gibt (katalog_key, positionen) zurück.
    """
    treatment_name = tooth.get("treatment_name", "")
    is_implant     = tooth.get("is_implant", False)
    goz_basis      = tooth.get("goz_basis")
    karies_fl      = tooth.get("karies_flaechen")

    key = find_katalog_key(treatment_name, is_implant, goz_basis)
    if not key:
        return None, []

    variante = None
    if key == "Inlay_Cerec":
        variante = get_inlay_variante(karies_fl, goz_basis)

    positionen = get_template(key, variante, katalog) or []
    return key, positionen


def template_to_prompt_str(positionen: list[dict]) -> str:
    """
    Formatiert eine Stückliste als kompakten Prompt-String für den GOZ-Agenten.
    """
    if not positionen:
        return ""
    lines = ["## Praxis-Stückliste (aus Katalog – bitte prüfen und anpassen)"]
    kat_icon = {"pflicht": "🟢", "empfohlen": "🟡", "optional": "⬜", "session": "🔁"}
    for pos in positionen:
        icon = kat_icon.get(pos.get("kategorie", "optional"), "⬜")
        sess = " [Session-einmalig]" if pos.get("session_einmalig") else ""
        lines.append(
            f"  {icon} {pos['goz_nr']} – {pos.get('text','')}"
            f" | ×{pos.get('avg_faktor',2.3):.1f} | {pos.get('kategorie','?')}{sess}"
            f" ({pos.get('haeufigkeit_pct',0):.0f}% der Fälle)"
        )
    lines.append("→ Übernehme diese Liste, passe sie an diesen spezifischen Patienten an.")
    return "\n".join(lines)
