"""
Dekodierung der befund01pa.zahn{N}-Strings (43 Zeichen).

Format:
  Z [code1][code2] [...Attribute...] [plan_flag@Pos24] [...]
  ^1  ^2    ^3       ^10              ^24

Pos 1-2 = Kronentyp-Code (0-indexed: s[1:3])
Pos 10  = 'G' → Zahn steht auf einem Implantat (Implantat-Träger)
Pos 24  = '2' → neu geplant in diesem KV (nicht nur IST-Befund)
"""
from config import (TOOTH_TREATMENT_CODES, GOZ_BASIS_KRONE,
                    IMPLANT_FLAG_CHAR, IMPLANT_FLAG_POS, IMPLANT_SUFFIX,
                    GOZ_BASIS_IMPLANTAT)

ALLE_ZAEHNE = [
    11, 12, 13, 14, 15, 16, 17, 18,
    21, 22, 23, 24, 25, 26, 27, 28,
    31, 32, 33, 34, 35, 36, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48,
]

LEER = "Z" + "0" * 42  # Leer-String = kein Befund


def decode_tooth_string(s: str | None) -> dict:
    """
    Dekodiert einen zahn{N}-String aus befund01pa.

    Returns:
        {
          "has_treatment": bool,
          "code": str,          # "08", "04", etc.
          "treatment_name": str,
          "is_new_plan": bool,  # True wenn Pos 24 == '2'
          "is_implant": bool,   # True wenn Pos 10 == 'G'
          "goz_basis": str | None,
          "raw": str
        }
    """
    empty = {
        "has_treatment": False,
        "code": "00",
        "treatment_name": "–",
        "is_new_plan": False,
        "is_implant": False,
        "goz_basis": None,
        "raw": s or LEER,
    }
    if not s or s == LEER or len(s) < 3:
        return empty

    code = s[1:3]
    is_new_plan  = len(s) > 23 and s[23] == "2"
    is_implant   = len(s) > IMPLANT_FLAG_POS and s[IMPLANT_FLAG_POS] == IMPLANT_FLAG_CHAR

    base_name    = TOOTH_TREATMENT_CODES.get(code, "")
    treatment_name = (base_name or f"Behandlung (Code {code})")
    if is_implant:
        treatment_name += IMPLANT_SUFFIX

    # GOZ-Basis: Implantat überschreibt Standard-Krone
    if is_implant:
        goz_basis = GOZ_BASIS_IMPLANTAT
    else:
        goz_basis = GOZ_BASIS_KRONE.get(code)

    has_treatment = (code != "00") or (s != LEER)

    return {
        "has_treatment": has_treatment and s != LEER,
        "code": code,
        "treatment_name": treatment_name,
        "is_new_plan": is_new_plan,
        "is_implant": is_implant,
        "goz_basis": goz_basis,
        "raw": s,
    }


def extract_planned_teeth(befund01pa_row: dict) -> list[dict]:
    """
    Extrahiert alle Zähne mit NEU GEPLANTER Behandlung (Pos 24 == '2').

    Returns list of:
        {
          "zahn": int,       # FDI-Nummer
          "code": str,
          "treatment_name": str,
          "goz_basis": str | None,
        }
    """
    planned = []
    for zahn_nr in ALLE_ZAEHNE:
        col = f"zahn{zahn_nr}"
        s = befund01pa_row.get(col, "")
        decoded = decode_tooth_string(s)
        if decoded["is_new_plan"] and decoded["code"] not in ("00", ""):
            planned.append({
                "zahn": zahn_nr,
                "code": decoded["code"],
                "treatment_name": decoded["treatment_name"],
                "is_implant": decoded["is_implant"],
                "goz_basis": decoded["goz_basis"],
                "raw": decoded["raw"],
            })
    return planned


def extract_all_tooth_status(befund01pa_row: dict) -> list[dict]:
    """Gibt alle Zähne mit irgendeinem Befund zurück (für Übersicht)."""
    result = []
    for zahn_nr in ALLE_ZAEHNE:
        col = f"zahn{zahn_nr}"
        s = befund01pa_row.get(col, "")
        decoded = decode_tooth_string(s)
        if decoded["has_treatment"]:
            result.append({
                "zahn": zahn_nr,
                **decoded
            })
    return result


def find_goz_gap(planned_teeth: list[dict], kv_positionen: list[dict]) -> list[dict]:
    """
    Vergleicht geplante Zähne (aus befund01pa) mit vorhandenen GOZ-Einträgen
    und gibt diejenigen Zähne zurück, für die noch KEINE GOZ vorhanden ist.

    kv_positionen: Ergebnis aus db.get_kv_details()["positionen"]
    """
    from db import bitmask_to_fdi

    # Zähne die bereits GOZ-Einträge haben
    zaehne_mit_goz = set()
    for pos in kv_positionen:
        bitmask = pos.get("zahn_bitmask") or 0
        for fdi in bitmask_to_fdi(bitmask):
            zaehne_mit_goz.add(fdi)

    gap = [t for t in planned_teeth if t["zahn"] not in zaehne_mit_goz]
    return gap
