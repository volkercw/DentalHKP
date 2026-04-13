"""Datenbankzugriff für DentalHKP – alle Abfragen an die Charly-DB"""
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from config import DB_CONFIG

# ─────────────────────────────────────────────────────────────────────────────
# Verbindung
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def get_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def _fetchall_dict(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# Patientensuche
# ─────────────────────────────────────────────────────────────────────────────

def search_patients(query: str, limit: int = 50) -> list[dict]:
    """
    Flexibler Patientensuche – unterstützt alle Eingabeformate:
      'Christ'            → Nachname-Suche
      'Waldemar'          → Vorname-Suche
      'Christ Waldemar'   → Nachname + Vorname (beliebige Reihenfolge)
      'Waldemar Christ'   → Vorname + Nachname
      'Christ, Waldemar'  → Komma als Trenner
      'Chr Wal'           → Teilstrings beider Namen
    Sortierung: exakte Wortstamm-Treffer zuerst, dann alphab.
    """
    import re as _re
    # Normalisierung: Komma/Semikolon/mehrfache Leerzeichen → einzelnes Leerzeichen
    q_norm = _re.sub(r'[,;/]+', ' ', query).strip()
    parts  = [p for p in q_norm.split() if p]

    _base = """
        SELECT solid, name, vorname,
               to_char(('J' || geb_datum)::date + 1, 'DD.MM.YYYY') AS geburtsdatum"""

    with get_connection() as conn:
        with conn.cursor() as cur:
            if len(parts) >= 2:
                # Zweiteilig: alle Reihenfolgen (Name+Vorname / Vorname+Name)
                a   = f"%{parts[0]}%"
                b   = f"%{parts[1]}%"
                asw = f"{parts[0]}%"
                bsw = f"{parts[1]}%"
                sql = f"""{_base},
                    CASE
                        WHEN (name ILIKE %s AND vorname ILIKE %s)
                          OR (name ILIKE %s AND vorname ILIKE %s) THEN 0
                        ELSE 1
                    END AS sort_prio
                FROM public.patienten
                WHERE (name ILIKE %s AND vorname ILIKE %s)
                   OR (name ILIKE %s AND vorname ILIKE %s)
                ORDER BY sort_prio, name, vorname
                LIMIT %s"""
                params = (
                    asw, bsw,   # sort_prio 0: Name↔Vorname Wortstamm-Match
                    bsw, asw,   # sort_prio 0: Vorname↔Name Wortstamm-Match
                    a, b,       # WHERE: combo 1
                    b, a,       # WHERE: combo 2
                    limit,
                )
            else:
                # Einzelwort: in Name ODER Vorname (Wortstamm zuerst)
                starts_with = f"{parts[0]}%" if parts else f"%{query}%"
                contains    = f"%{query}%"
                sql = f"""{_base},
                    CASE WHEN name ILIKE %s OR vorname ILIKE %s THEN 0
                         ELSE 1 END AS sort_prio
                FROM public.patienten
                WHERE name ILIKE %s OR vorname ILIKE %s
                ORDER BY sort_prio, name, vorname
                LIMIT %s"""
                params = (starts_with, starts_with, contains, contains, limit)

            cur.execute(sql, params)
            rows = _fetchall_dict(cur)
            # sort_prio ist nur intern, nicht zurückgeben
            for r in rows:
                r.pop("sort_prio", None)
            return rows


def get_patient_by_id(patid: int) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT solid, name, vorname,
                    to_char(('J' || geb_datum)::date + 1, 'DD.MM.YYYY') AS geburtsdatum
                   FROM public.patienten WHERE solid = %s""",
                (patid,)
            )
            row = cur.fetchone()
            if row:
                return dict(zip([d[0] for d in cur.description], row))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# KV-Einträge
# ─────────────────────────────────────────────────────────────────────────────

def get_recent_kvs(patid: int = None, limit: int = 30) -> list[dict]:
    """
    Gibt die neuesten KV-Einträge zurück.
    Wenn patid angegeben: nur für diesen Patienten.
    Sonst: global die letzten N KVs (alle Patienten).
    """
    if patid:
        sql = """
            SELECT kv.solid, kv.patid, kv.kurztext, kv.kvstatus,
                to_char(('J' || kv.datum)::date + 1, 'DD.MM.YYYY') AS datum,
                kv.honorar, kv.material, kv.labor,
                p.name || ', ' || p.vorname AS patient_name
            FROM public.kv
            JOIN public.patienten p ON p.solid = kv.patid
            WHERE kv.patid = %s
            ORDER BY kv.solid DESC
            LIMIT %s
        """
        params = (patid, limit)
    else:
        sql = """
            SELECT kv.solid, kv.patid, kv.kurztext, kv.kvstatus,
                to_char(('J' || kv.datum)::date + 1, 'DD.MM.YYYY') AS datum,
                kv.honorar, kv.material, kv.labor,
                p.name || ', ' || p.vorname AS patient_name
            FROM public.kv
            JOIN public.patienten p ON p.solid = kv.patid
            WHERE ('J' || kv.datum)::date + 1 >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY kv.solid DESC
            LIMIT %s
        """
        params = (limit,)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return _fetchall_dict(cur)


def get_kv_details(kv_solid: int) -> dict | None:
    """Lädt einen KV-Kopf mit allen kv_main/kv_daten-Positionen."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # KV-Kopf
            cur.execute(
                """SELECT kv.*,
                       p.name || ', ' || p.vorname AS patient_name,
                       to_char(('J' || kv.datum)::date + 1, 'DD.MM.YYYY') AS datum_str
                   FROM public.kv JOIN public.patienten p ON p.solid=kv.patid
                   WHERE kv.solid=%s""",
                (kv_solid,)
            )
            kv = _fetchall_dict(cur)
            if not kv:
                return None
            kv = kv[0]

            # kv_main + kv_daten
            cur.execute(
                """SELECT km.solid AS km_id, km.lfdnr, km.zahn AS zahn_bitmask,
                       km.bezeichnung AS phase_bezeichnung,
                       kd.solid AS kd_id, kd.lfdnr AS pos_nr,
                       kd.nummer AS goz_nr, kd.bezeichnung AS goz_text,
                       kd.mp AS faktor, kd.betrag, kd.anzahl,
                       kd.fuellungszahn, kd.fuellungslage
                   FROM public.kv_main km
                   LEFT JOIN public.kv_daten kd ON kd.kvmainid = km.solid
                   WHERE km.kvid = %s
                   ORDER BY km.lfdnr, kd.lfdnr""",
                (kv_solid,)
            )
            kv["positionen"] = _fetchall_dict(cur)
            # datum_str überschreibt datum für Anzeige
            if "datum_str" in kv:
                kv["datum"] = kv.pop("datum_str")
            return kv


# ─────────────────────────────────────────────────────────────────────────────
# Grafische Planungsdaten (befund01pa)
# ─────────────────────────────────────────────────────────────────────────────

def get_tooth_plan_for_kv(kv_solid: int) -> dict | None:
    """
    Lädt den befund01pa-Eintrag der zum KV gehört (befundkv=N).
    Verknüpfung: kv_rank = Reihenfolge dieser KV für patid+datum.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Rang dieser KV innerhalb patid+datum
            cur.execute(
                """WITH ranked AS (
                    SELECT solid,
                        ROW_NUMBER() OVER (PARTITION BY patid, datum ORDER BY solid) AS kv_rank
                    FROM public.kv
                    WHERE solid = %s
                )
                SELECT kv.patid, kv.datum, ranked.kv_rank
                FROM public.kv
                JOIN ranked ON ranked.solid = kv.solid
                WHERE kv.solid = %s""",
                (kv_solid, kv_solid)
            )
            row = cur.fetchone()
            if not row:
                return None
            patid, datum, kv_rank = row

            # befund01pa mit befundkv = kv_rank
            cur.execute(
                """SELECT * FROM public.befund01pa
                   WHERE patid = %s AND datum = %s AND befundkv = %s
                   ORDER BY solid DESC LIMIT 1""",
                (patid, datum, kv_rank)
            )
            rows = _fetchall_dict(cur)
            return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────────────────────
# Historische GOZ-Muster (für Archiv-Agent)
# ─────────────────────────────────────────────────────────────────────────────

def get_historical_goz_for_treatment(behandlung_typ: str, limit_kvs: int = 200) -> list[dict]:
    """
    Findet die häufigsten GOZ-Positionen aus historischen KVs
    die denselben Behandlungstyp (z.B. 'Keramikkrone', 'Kunststoffkrone') beinhalten.
    """
    # Suchbegriff je Behandlungstyp
    search_map = {
        "Keramikkrone": "%Keramik%Vollkrone%",
        "Kunststoffkrone": "%Kunststoff%Vollkrone%",
        "Verblendkrone": "%Verblend%",
        "Metallkrone": "%Metall%Vollkrone%",
        "Inlay": "%Inlay%",
        "Cerec": "%Inlay%",
        "Onlay": "%Onlay%",
    }
    pattern = next(
        (v for k, v in search_map.items() if k.lower() in behandlung_typ.lower()),
        f"%{behandlung_typ}%"
    )

    sql = """
        WITH similar_kvs AS (
            SELECT DISTINCT km.kvid
            FROM public.kv_main km
            WHERE km.bezeichnung ILIKE %s
            ORDER BY km.kvid DESC
            LIMIT %s
        )
        SELECT
            kd.nummer            AS goz_nr,
            MAX(kd.bezeichnung)  AS bezeichnung,
            COUNT(*)             AS haeufigkeit,
            ROUND(AVG(kd.mp)::numeric, 2) AS avg_faktor,
            ROUND(AVG(kd.betrag)::numeric, 2) AS avg_betrag
        FROM public.kv_daten kd
        JOIN public.kv_main km ON km.solid = kd.kvmainid
        WHERE km.kvid IN (SELECT kvid FROM similar_kvs)
          AND kd.nummer IS NOT NULL
          AND kd.nummer != ''
        GROUP BY kd.nummer
        ORDER BY haeufigkeit DESC
        LIMIT 30
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pattern, limit_kvs))
            return _fetchall_dict(cur)


def get_praxis_analog_positionen() -> list[dict]:
    """§6-Analog-Positionen dieser Praxis (nicht im GOZ-Standard)."""
    sql = """
        SELECT DISTINCT nummer, MAX(bezeichnung) AS bezeichnung, COUNT(*) AS haeufigkeit
        FROM public.kv_daten
        WHERE nummer NOT IN (SELECT nummer FROM public.goz WHERE nummer IS NOT NULL)
          AND nummer NOT LIKE 'Ä%'
          AND nummer IS NOT NULL AND nummer != '' AND nummer != '_'
        GROUP BY nummer
        ORDER BY haeufigkeit DESC
        LIMIT 20
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return _fetchall_dict(cur)


def get_goz_info(goz_nr: str) -> dict | None:
    """Lädt Stammdaten und Praxis-Faktor einer GOZ-Position."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT g.solid, g.nummer, g.goztext,
                       gd.mp AS praxis_faktor
                   FROM public.goz g
                   LEFT JOIN public.gozdaten gd ON gd.gozid = g.solid
                   WHERE g.nummer = %s
                   LIMIT 1""",
                (goz_nr,)
            )
            rows = _fetchall_dict(cur)
            return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────────────────────
# Material- und Labor-Daten
# ─────────────────────────────────────────────────────────────────────────────

def get_kv_material(kv_solid: int) -> list[dict]:
    """
    Lädt Material-Positionen für einen KV.
    Verknüpft kv_material → kv_main → kv_daten.
    """
    sql = """
        SELECT
            km.bezeichnung      AS phase,
            kd.nummer           AS goz_nr,
            kd.bezeichnung      AS goz_text,
            km2.lfdnr,
            km2.anzahl,
            km2.nummer          AS mat_nr,
            km2.kuerzel         AS mat_kuerzel,
            km2.bezeichnung     AS mat_bez,
            km2.betrag
        FROM public.kv_material km2
        JOIN public.kv_main km ON km.solid = km2.kvmainid
        LEFT JOIN public.kv_daten kd ON kd.solid = km2.kvdatenid
        WHERE km.kvid = %s
        ORDER BY km.lfdnr, km2.lfdnr
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (kv_solid,))
            return _fetchall_dict(cur)


def get_kv_labor_summary(kv_solid: int) -> dict:
    """
    Lädt Labor-Zusammenfassung (Summen) und detaillierte Labor-Positionen.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Gesamtsummen
            cur.execute(
                "SELECT * FROM public.kv_labor WHERE kvid = %s LIMIT 1",
                (kv_solid,)
            )
            rows = _fetchall_dict(cur)
            summary = rows[0] if rows else {}

            # Detaillierte Leistungen
            cur.execute(
                """SELECT lfdnr, nummer, kuerzel, bezeichnung, anzahl, betrag, laborart, eigenfremd
                   FROM public.kvlaborleistung WHERE kvid = %s ORDER BY lfdnr""",
                (kv_solid,)
            )
            leistungen = _fetchall_dict(cur)

            # Labor-Material
            cur.execute(
                """SELECT lfdnr, nummer, kuerzel, bezeichnung, anzahl, betrag
                   FROM public.kvlabormaterial WHERE kvid = %s ORDER BY lfdnr""",
                (kv_solid,)
            )
            materialien = _fetchall_dict(cur)

    return {
        "summary": summary,
        "leistungen": leistungen,
        "materialien": materialien,
    }


def get_goz_praxis_categories(limit: int = 40) -> list[dict]:
    """
    Gibt die häufigsten Behandlungsphasen-Namen zurück (kv_main.bezeichnung).
    Diese dienen als Kategorien im GOZ-Selektor.
    """
    sql = """
        SELECT bezeichnung, COUNT(*) AS cnt
        FROM public.kv_main
        WHERE bezeichnung IS NOT NULL AND bezeichnung != ''
          AND LENGTH(bezeichnung) BETWEEN 2 AND 30
        GROUP BY bezeichnung
        ORDER BY cnt DESC
        LIMIT %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return _fetchall_dict(cur)


def get_goz_by_category(category: str, limit: int = 60) -> list[dict]:
    """
    Gibt die häufigsten GOZ-Positionen zurück die in einer bestimmten
    Behandlungsphase (kv_main.bezeichnung) verwendet werden.
    """
    sql = """
        SELECT
            kd.nummer,
            MAX(kd.bezeichnung)         AS bezeichnung,
            COUNT(*)                    AS haeufigkeit,
            ROUND(AVG(kd.mp)::numeric, 2) AS avg_faktor
        FROM public.kv_daten kd
        JOIN public.kv_main km ON km.solid = kd.kvmainid
        WHERE km.bezeichnung = %s
          AND kd.nummer IS NOT NULL AND kd.nummer != ''
        GROUP BY kd.nummer
        ORDER BY haeufigkeit DESC
        LIMIT %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (category, limit))
            return _fetchall_dict(cur)


def search_goz_volltext(query: str, limit: int = 60) -> list[dict]:
    """
    Volltext-Suche über alle GOZ-Nummern und Beschreibungen.
    Durchsucht kv_daten (praxiseigene Bezeichnungen) und goz-Stammdaten.
    """
    pattern = f"%{query}%"
    sql = """
        SELECT
            kd.nummer,
            MAX(kd.bezeichnung)           AS bezeichnung,
            COUNT(*)                      AS haeufigkeit,
            ROUND(AVG(kd.mp)::numeric, 2) AS avg_faktor
        FROM public.kv_daten kd
        WHERE (kd.bezeichnung ILIKE %s OR kd.nummer ILIKE %s)
          AND kd.nummer IS NOT NULL AND kd.nummer != ''
        GROUP BY kd.nummer
        ORDER BY haeufigkeit DESC
        LIMIT %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pattern, pattern, limit))
            return _fetchall_dict(cur)


def lookup_goz_nr(goz_nr: str) -> dict | None:
    """
    Sucht eine GOZ-Nummer in der Datenbank und gibt Stammdaten zurück.
    Sucht auch in kv_daten (für §6-Analog und praxisspez. Positionen).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Zuerst in GOZ-Stammdaten
            cur.execute(
                """SELECT g.nummer, g.goztext AS bezeichnung,
                       gd.mp AS praxis_faktor
                   FROM public.goz g
                   LEFT JOIN public.gozdaten gd ON gd.gozid = g.solid
                   WHERE g.nummer = %s LIMIT 1""",
                (goz_nr,)
            )
            rows = _fetchall_dict(cur)
            if rows:
                return rows[0]

            # Fallback: häufigste Bezeichnung aus kv_daten
            cur.execute(
                """SELECT nummer, MAX(bezeichnung) AS bezeichnung,
                       ROUND(AVG(mp)::numeric, 2) AS praxis_faktor
                   FROM public.kv_daten
                   WHERE nummer = %s
                   GROUP BY nummer LIMIT 1""",
                (goz_nr,)
            )
            rows = _fetchall_dict(cur)
            return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────────────────────
# GOZ-Preisermittlung für Kostenvoranschlag
# ─────────────────────────────────────────────────────────────────────────────

GOZ_PUNKTWERT = 0.0562421  # GOZ 2012, Bundeseinheitlicher Punktwert


def get_goz_prices_bulk(goz_nrs: list[str]) -> dict:
    """
    Gibt Preis-Stammdaten für mehrere GOZ-Nummern zurück (Batch-Query).

    Returns: {
        goz_nr: {
            "goztext": str,
            "base_fee": float,   # Basishonorar bei Faktor 1.0 (Punktzahl × Bewertung × Punktwert)
            "mat_est": float,    # Materialschätzwert aus GOZ-Stamm
            "lab_est": float,    # Laborschätzwert (Fremdlabor + Gold) aus GOZ-Stamm
        }
    }
    §6-Analog-Positionen nicht im GOZ-Stamm → historischer Durchschnitt als Fallback.
    """
    if not goz_nrs:
        return {}

    unique_nrs = list(set(goz_nrs))
    result = {}

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Aus GOZ-Stammdaten (Punktzahl × Bewertung × Punktwert)
            placeholders = ",".join(["%s"] * len(unique_nrs))
            cur.execute(
                f"""SELECT g.nummer, g.goztext,
                           gd.punktzahl, gd.bewertung,
                           g.schaetzbetrag,
                           g.fremdschaetzbetr, g.fremdgoldbetr
                    FROM public.goz g
                    LEFT JOIN public.gozdaten gd ON gd.gozid = g.solid
                    WHERE g.nummer IN ({placeholders})""",
                unique_nrs,
            )
            for row in cur.fetchall():
                nr, text, punkte, bew, mat, fremdscha, fremdgold = row
                punkte = float(punkte or 0)
                bew    = float(bew or 1)
                base   = punkte * bew * GOZ_PUNKTWERT
                result[nr] = {
                    "goztext":  text or "",
                    "base_fee": base,
                    "mat_est":  float(mat or 0),
                    "lab_est":  float(fremdscha or 0) + float(fremdgold or 0),
                }

            # Fallback für §6-Analog / praxisspez. Positionen
            missing = [nr for nr in unique_nrs if nr not in result]
            if missing:
                ph2 = ",".join(["%s"] * len(missing))
                cur.execute(
                    f"""SELECT nummer,
                               AVG(betrag::numeric)  AS avg_b,
                               AVG(mp::numeric)      AS avg_mp
                        FROM public.kv_daten
                        WHERE nummer IN ({ph2})
                          AND betrag::numeric > 0
                          AND mp::numeric     > 0
                        GROUP BY nummer""",
                    missing,
                )
                for row in cur.fetchall():
                    nr, avg_b, avg_mp = row
                    avg_b  = float(avg_b  or 0)
                    avg_mp = float(avg_mp or 1)
                    result[nr] = {
                        "goztext":  f"§6-Analog / Praxis ({nr})",
                        "base_fee": avg_b / avg_mp if avg_mp > 0 else avg_b,
                        "mat_est":  0.0,
                        "lab_est":  0.0,
                    }

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Kariesbefund aus befundze
# ─────────────────────────────────────────────────────────────────────────────

# Alle 32 FDI-Zahnummern die in befundze als Spalten existieren
_BEFUNDZE_ZAEHNE = [
    11, 12, 13, 14, 15, 16, 17, 18,
    21, 22, 23, 24, 25, 26, 27, 28,
    31, 32, 33, 34, 35, 36, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48,
]


def get_befundze_for_kv(kv_solid: int) -> dict:
    """
    Liest den Kariestatus aus befundze für den Patienten/Datum des KV.

    befundze speichert Karies-Flags je Zahn (z11–z48, Integer-Bitmask).
    Bit-Bedeutung (typisch Charly):
      bit 0 (1)  = mesiale Fläche
      bit 1 (2)  = distale Fläche
      bit 2 (4)  = okklusale Fläche
      bit 3 (8)  = vestibuläre Fläche
      bit 4 (16) = orale / palatinale Fläche

    Returns:
        {fdi_nr: {"wert": int, "flaechen": int, "flaechen_text": str}}
        Nur Zähne mit Karies (wert > 0) werden zurückgegeben.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Patid + Datum aus KV
            cur.execute(
                "SELECT patid, datum FROM public.kv WHERE solid = %s",
                (kv_solid,)
            )
            row = cur.fetchone()
            if not row:
                return {}
            patid, datum = row

            # befundze für diesen Patienten/Datum – neueste Zeile
            cols_sql = ", ".join(f"z{nr}" for nr in _BEFUNDZE_ZAEHNE)
            cur.execute(
                f"SELECT {cols_sql} FROM public.befundze "
                f"WHERE patid = %s AND datum = %s ORDER BY solid DESC LIMIT 1",
                (patid, datum)
            )
            bze_row = cur.fetchone()
            if not bze_row is None:
                pass
            else:
                # Fallback: neueste befundze für diesen Patienten
                cur.execute(
                    f"SELECT {cols_sql} FROM public.befundze "
                    f"WHERE patid = %s ORDER BY datum DESC, solid DESC LIMIT 1",
                    (patid,)
                )
                bze_row = cur.fetchone()

            if not bze_row:
                return {}

            result = {}
            for i, zahn_nr in enumerate(_BEFUNDZE_ZAEHNE):
                wert = bze_row[i] or 0
                # Karies-Bits: nur die unteren 8 Bits (0xFF).
                # Höhere Bits (z.B. 4096, 20480) markieren bestehende Restaurationen,
                # keine aktive Karies.
                karies_wert = wert & 0xFF
                if karies_wert > 0:
                    flaechen_parts = []
                    if karies_wert & 1:  flaechen_parts.append("mes")
                    if karies_wert & 2:  flaechen_parts.append("dis")
                    if karies_wert & 4:  flaechen_parts.append("okk")
                    if karies_wert & 8:  flaechen_parts.append("ves")
                    if karies_wert & 16: flaechen_parts.append("ora")
                    if karies_wert & 32: flaechen_parts.append("ves2")
                    if karies_wert & 64: flaechen_parts.append("okk2")
                    flaechen = len(flaechen_parts) or bin(karies_wert).count("1")
                    result[zahn_nr] = {
                        "wert":          karies_wert,
                        "wert_gesamt":   wert,
                        "flaechen":      flaechen,
                        "flaechen_text": "+".join(flaechen_parts) or str(karies_wert),
                    }
            return result


def caries_to_treatment(zahn_nr: int, caries_info: dict) -> dict:
    """
    Wandelt einen Kariesbefund in einen geplanten Behandlungs-Eintrag um
    (kompatibel mit dem Format aus extract_planned_teeth()).

    Inlay-Auswahl nach Flächenanzahl:
        1 Fläche  → GOZ 2180 (einflächig)
        2 Flächen → GOZ 2190 (zweiflächig)
        3+ Flächen → GOZ 2200 (dreiflächig)
    """
    flaechen = caries_info.get("flaechen", 1)
    flaechen_text = caries_info.get("flaechen_text", "")

    if flaechen <= 1:
        goz_basis = "2180"
        typ_text  = "einflächig"
    elif flaechen == 2:
        goz_basis = "2190"
        typ_text  = "zweiflächig"
    else:
        goz_basis = "2200"
        typ_text  = "dreiflächig"

    return {
        "zahn":           zahn_nr,
        "code":           "karies",
        "treatment_name": f"Keramik-Inlay / Cerec ({typ_text}, {flaechen_text})",
        "is_implant":     False,
        "is_new_plan":    True,
        "goz_basis":      goz_basis,
        "source":         "karies_befundze",
        "karies_flaechen": flaechen,
        "karies_wert":    caries_info.get("wert", 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dekodierung Zahnbitmask (kv_main.zahn → FDI-Liste)
# ─────────────────────────────────────────────────────────────────────────────

BIT_TO_FDI = {
    0: 18, 1: 17, 2: 16, 3: 15, 4: 14, 5: 13, 6: 12, 7: 11,
    8: 21, 9: 22, 10: 23, 11: 24, 12: 25, 13: 26, 14: 27, 15: 28,
    16: 31, 17: 32, 18: 33, 19: 34, 20: 35, 21: 36, 22: 37, 23: 38,
    24: 41, 25: 42, 26: 43, 27: 44, 28: 45, 29: 46,
}

def bitmask_to_fdi(bitmask: int) -> list[int]:
    """Wandelt kv_main.zahn Bitmask in FDI-Zahnliste um."""
    if not bitmask:
        return []
    # Unsigned 32-bit
    if bitmask < 0:
        bitmask = bitmask & 0xFFFFFFFF
    return [fdi for bit, fdi in BIT_TO_FDI.items() if bitmask & (1 << bit)]
