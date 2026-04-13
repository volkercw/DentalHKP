"""
Freitext-Behandlungsplanung – Parser + Validator

Workflow:
  1. parse_treatment_text()   → Claude extrahiert Zähne aus Freitext
  2. validate_parsed_teeth()  → Regel-Checks gegen befund01pa / befundze / kv_daten
  3. apply_correction()       → Nutzer-Korrekturen auf unklare Einträge anwenden

Konfidenz-Stufen:
  "ok"       → Zahn + Behandlung eindeutig, kein Konflikt
  "unclear"  → Etwas fehlt oder ist mehrdeutig (Rückfrage nötig)
  "conflict" → Widerspruch zu befund01pa oder bestehenden GOZ
"""

import json
import re
import anthropic
from config import CLAUDE_MODEL, ANTHROPIC_API_KEY
from katalog_builder import find_katalog_key, get_inlay_variante, BEHANDLUNGEN_CONFIG

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

CONFIDENCE_OK       = "ok"
CONFIDENCE_UNCLEAR  = "unclear"
CONFIDENCE_CONFLICT = "conflict"

# Gültige FDI-Zahnummern
VALID_FDI = frozenset([
    11,12,13,14,15,16,17,18,
    21,22,23,24,25,26,27,28,
    31,32,33,34,35,36,37,38,
    41,42,43,44,45,46,47,48,
])

# Behandlungstyp → kanonischer Name + GOZ-Basis
BEHANDLUNG_LOOKUP: dict[str, dict] = {
    k: {"name": v["bezeichnung"], "goz_basis": v["haupt_goz"],
        "implant": v["implant"]}
    for k, v in BEHANDLUNGEN_CONFIG.items()
}


# ─────────────────────────────────────────────────────────────────────────────
# Schritt 1: Claude-Parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_treatment_text(
    text: str,
    patient_name: str = "",
) -> list[dict]:
    """
    Claude parst Freitext → strukturierte Zahn-Liste.
    Gibt Liste zurück, auch bei Fehlern (dann confidence="unclear").
    """
    system = (
        "Du bist Assistent in einer Zahnarztpraxis und hilfst beim Parsen von "
        "Behandlungsnotizen. Extrahiere Zähne und geplante Behandlungen. "
        "Antworte AUSSCHLIESSLICH mit validem JSON-Array."
    )

    behandlungs_typen = "\n".join(
        f'  "{k}": {v["bezeichnung"]}'
        for k, v in BEHANDLUNGEN_CONFIG.items()
    )

    user_msg = f"""Parse diese Behandlungsnotiz{f" für {patient_name}" if patient_name else ""}:
\"{text}\"

Extrahiere jeden erwähnten Zahn. Für jeden Zahn:
- zahn: FDI-Nummer als Integer (z.B. 31), oder null wenn unklar
- behandlung_raw: wörtliche Behandlungsangabe aus dem Text
- katalog_key: passender Behandlungstyp aus dieser Liste:
{behandlungs_typen}
  Wenn unklar → null
- is_implant: true wenn "Implantat", "Impl.", "impl" erwähnt UND Prothetik gemeint ist
  (NICHT bei rein chirurgischer Implantat-Setzung)
- goz_basis: "2210" für Kronen, "2200i" für Implantatkrone, "2190" für Inlay (default),
  "2180"/"2200" wenn Flächen bekannt, sonst null
- karies_flaechen: Integer (1-5) wenn Flächenanzahl erwähnt, sonst null
- confidence: "ok" wenn Zahn+Behandlung klar, "unclear" wenn etwas fehlt/mehrdeutig
- hinweis: kurze Erklärung wenn confidence="unclear", sonst ""

Beispiele:
- "Zahn 31 Implantat Keramik" → zahn:31, katalog_key:"Keramikkrone_Implantat", is_implant:true
- "11 Vollkeramik" → zahn:11, katalog_key:"Keramikkrone", is_implant:false
- "35 Inlay 2-flächig" → zahn:35, katalog_key:"Inlay_Cerec", karies_flaechen:2, goz_basis:"2190"
- "Keramikkrone" (ohne Zahnummer) → zahn:null, confidence:"unclear", hinweis:"Zahnummer fehlt"
- "VMK 14" → zahn:14, katalog_key:"Verblendkrone"
- "Zähne 11, 21 Keramik" → zwei Einträge: zahn:11 UND zahn:21

Antworte NUR mit dem JSON-Array:
[{{"zahn":31,"behandlung_raw":"...","katalog_key":"...","is_implant":false,
   "goz_basis":"2210","karies_flaechen":null,"confidence":"ok","hinweis":""}}]"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text if response.content else "[]"

        # JSON-Array extrahieren
        m = re.search(r'\[[\s\S]*\]', raw)
        if m:
            parsed = json.loads(m.group(0))
        else:
            parsed = []

        # Fehlende Pflichtfelder mit Defaults füllen
        result = []
        for item in parsed:
            result.append({
                "zahn":            item.get("zahn"),
                "behandlung_raw":  item.get("behandlung_raw", ""),
                "katalog_key":     item.get("katalog_key"),
                "is_implant":      bool(item.get("is_implant", False)),
                "goz_basis":       item.get("goz_basis"),
                "karies_flaechen": item.get("karies_flaechen"),
                "confidence":      item.get("confidence", CONFIDENCE_UNCLEAR),
                "hinweis":         item.get("hinweis", ""),
                "source":          "texteingabe",
                # Abgeleitete Felder werden in validate_parsed_teeth() gesetzt
                "treatment_name":  "",
                "conflict_detail": "",
            })
        return result

    except Exception as e:
        return [{
            "zahn":            None,
            "behandlung_raw":  text[:80],
            "katalog_key":     None,
            "is_implant":      False,
            "goz_basis":       None,
            "karies_flaechen": None,
            "confidence":      CONFIDENCE_UNCLEAR,
            "hinweis":         f"Parser-Fehler: {e}",
            "source":          "texteingabe",
            "treatment_name":  "",
            "conflict_detail": "",
        }]


# ─────────────────────────────────────────────────────────────────────────────
# Schritt 2: Regel-basierter Validator
# ─────────────────────────────────────────────────────────────────────────────

def validate_parsed_teeth(
    parsed: list[dict],
    tooth_plan: dict | None,
    karies_befund: dict,
    kv_positionen: list[dict],
) -> list[dict]:
    """
    Prüft jedes geparste Element gegen Charly-Daten und setzt
    confidence + hinweis + treatment_name + conflict_detail.

    Checks:
      A  FDI-Nummer gültig?
      B  Behandlungstyp bekannt?
      C  Konflikt: Zahn bereits in befund01pa geplant?
      D  Konflikt: Zahn hat bereits GOZ-Einträge?
      E  Inlay: Flächenanzahl bekannt?
      F  Implantat: Krone oder Neuimplantat?
      G  Karies in befundze für diesen Zahn → Hinweis
    """
    import db as db_module
    from tooth_decoder import extract_planned_teeth

    # Bestehende geplante Zähne aus befund01pa
    already_planned: dict[int, str] = {}
    if tooth_plan:
        for t in extract_planned_teeth(tooth_plan):
            already_planned[t["zahn"]] = t["treatment_name"]

    # Zähne die bereits GOZ haben
    zaehne_mit_goz: set[int] = set()
    for pos in kv_positionen:
        for fdi in db_module.bitmask_to_fdi(pos.get("zahn_bitmask") or 0):
            zaehne_mit_goz.add(fdi)

    result = []
    for item in parsed:
        item = dict(item)   # Kopie
        issues = []
        conf = CONFIDENCE_OK

        zahn = item.get("zahn")
        key  = item.get("katalog_key")

        # ── A: FDI-Nummer ────────────────────────────────────────────────────
        if zahn is None:
            issues.append("Zahnummer fehlt")
            conf = CONFIDENCE_UNCLEAR
        elif int(zahn) not in VALID_FDI:
            issues.append(f"Ungültige FDI-Nummer: {zahn}")
            conf = CONFIDENCE_UNCLEAR
            zahn = None

        # ── B: Behandlungstyp ────────────────────────────────────────────────
        if not key:
            issues.append("Behandlungstyp unklar – Krone, Inlay oder anderes?")
            conf = CONFIDENCE_UNCLEAR
        else:
            cfg = BEHANDLUNGEN_CONFIG.get(key, {})
            # treatment_name aus Katalog-Config
            item["treatment_name"] = cfg.get("bezeichnung", key)
            # goz_basis auffüllen falls fehlt
            if not item.get("goz_basis"):
                item["goz_basis"] = cfg.get("haupt_goz")

        # ── C: Konflikt befund01pa ────────────────────────────────────────────
        if zahn and zahn in already_planned:
            existing = already_planned[zahn]
            item["conflict_detail"] = (
                f"Zahn {zahn} ist in Charly bereits geplant als: {existing}"
            )
            conf = CONFIDENCE_CONFLICT
            issues.append(f"Bereits geplant: {existing}")

        # ── D: Konflikt GOZ-Einträge ──────────────────────────────────────────
        elif zahn and zahn in zaehne_mit_goz and conf == CONFIDENCE_OK:
            item["conflict_detail"] = (
                f"Zahn {zahn} hat bereits GOZ-Einträge in diesem KV"
            )
            conf = CONFIDENCE_CONFLICT
            issues.append("Bestehende GOZ-Einträge gefunden")

        # ── E: Inlay ohne Flächenanzahl ──────────────────────────────────────
        if key == "Inlay_Cerec" and not item.get("karies_flaechen"):
            # Prüfen ob befundze Karies-Info hat
            if zahn and zahn in karies_befund:
                ci = karies_befund[zahn]
                item["karies_flaechen"] = ci["flaechen"]
                item["goz_basis"] = get_inlay_variante(ci["flaechen"], None).replace(
                    "1-flächig", "2180").replace("2-flächig", "2190").replace("3-flächig+", "2200")
                issues_inlay = (
                    f"Flächenanzahl aus Kariesbefund ergänzt: "
                    f"{ci['flaechen']} ({ci['flaechen_text']})"
                )
                # Kein Problem – nur Info
                if conf == CONFIDENCE_OK:
                    item["hinweis"] = issues_inlay
            else:
                issues.append("Inlay: Flächenanzahl unbekannt (1/2/3-flächig?)")
                if conf == CONFIDENCE_OK:
                    conf = CONFIDENCE_UNCLEAR
                item["goz_basis"] = item.get("goz_basis") or "2190"  # Default

        # ── F: Implantat-Typ ─────────────────────────────────────────────────
        if item.get("is_implant") and key not in ("Keramikkrone_Implantat",):
            # Implant-Flag gesetzt aber falscher key → korrigieren
            item["katalog_key"] = "Keramikkrone_Implantat"
            item["treatment_name"] = BEHANDLUNGEN_CONFIG["Keramikkrone_Implantat"]["bezeichnung"]
            item["goz_basis"] = "2200i"

        # ── G: Karies-Hinweis (Zahn hat Karies aber kein Inlay geplant) ──────
        if (zahn and zahn in karies_befund
                and key not in (None, "Inlay_Cerec")
                and conf != CONFIDENCE_CONFLICT):
            ci = karies_befund[zahn]
            item["hinweis"] = (item.get("hinweis") or "") + (
                f" ⚠️ befundze: Karies an Zahn {zahn} ({ci['flaechen_text']}) – "
                f"Inlay erwägen?"
            )

        # Finale Konfidenz & Hinweis
        item["confidence"] = conf
        if issues and conf != CONFIDENCE_OK:
            item["hinweis"] = (item.get("hinweis") or "") + "  ".join(issues)
        if not item.get("treatment_name"):
            item["treatment_name"] = item.get("behandlung_raw", "Unbekannt")

        result.append(item)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Schritt 3: Nutzer-Korrektur anwenden
# ─────────────────────────────────────────────────────────────────────────────

def apply_correction(
    item: dict,
    new_zahn: int | None = None,
    new_katalog_key: str | None = None,
    new_karies_flaechen: int | None = None,
    override_conflict: bool = False,
) -> dict:
    """
    Wendet Nutzer-Korrekturen auf ein unklares/konfliktbehaftetes Element an.
    Gibt korrigiertes Element zurück (noch nicht re-validiert).
    """
    item = dict(item)
    if new_zahn is not None:
        item["zahn"] = new_zahn
    if new_katalog_key is not None:
        item["katalog_key"] = new_katalog_key
        cfg = BEHANDLUNGEN_CONFIG.get(new_katalog_key, {})
        item["treatment_name"] = cfg.get("bezeichnung", new_katalog_key)
        item["goz_basis"]      = cfg.get("haupt_goz")
        item["is_implant"]     = cfg.get("implant", False)
    if new_karies_flaechen is not None:
        item["karies_flaechen"] = new_karies_flaechen
        goz_map = {1: "2180", 2: "2190"}
        item["goz_basis"] = goz_map.get(new_karies_flaechen, "2200")
    if override_conflict:
        item["confidence"]      = CONFIDENCE_OK
        item["conflict_detail"] = ""
        item["hinweis"]         = "⚠️ Konflikt vom Nutzer bestätigt"
    # Confidence zurücksetzen auf unclear für Re-Validierung
    elif item["confidence"] == CONFIDENCE_UNCLEAR:
        item["hinweis"] = ""
    return item


def to_gap_tooth(item: dict) -> dict:
    """
    Wandelt ein validiertes parsed-Element in das gap_teeth-Format um,
    das run_hkp_pipeline() erwartet.
    """
    key = item.get("katalog_key", "")
    cfg = BEHANDLUNGEN_CONFIG.get(key, {})
    return {
        "zahn":           item["zahn"],
        "code":           "text",
        "treatment_name": item.get("treatment_name", item.get("behandlung_raw", "")),
        "is_implant":     item.get("is_implant", False),
        "is_new_plan":    True,
        "goz_basis":      item.get("goz_basis") or cfg.get("haupt_goz"),
        "karies_flaechen": item.get("karies_flaechen"),
        "source":         "texteingabe",
        "raw":            item.get("behandlung_raw", ""),
    }


def summary_for_agent(parsed: list[dict]) -> str:
    """Kompakte Zusammenfassung der Text-Eingabe für Agent-Log."""
    lines = []
    for t in parsed:
        impl = " (Implantat)" if t.get("is_implant") else ""
        fl   = f" {t['karies_flaechen']}-fl." if t.get("karies_flaechen") else ""
        lines.append(f"  Zahn {t['zahn']}: {t['treatment_name']}{impl}{fl}")
    return "\n".join(lines)
