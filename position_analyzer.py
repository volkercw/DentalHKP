"""
Position-Analyse für GOZ-Positionen

Analysiert historische KV-Daten aus der Charly-DB und berechnet:

1. MKO-Häufigkeit und Co-Occurrence
   - Wie oft kommen MKO-Positionen (8000-8080) vor?
   - Welche GOZ-Nummern treten zusammen mit MKO auf (und wie oft)?
   - Schwellwert-Cluster: bei welchen Behandlungstypen gehört MKO dazu?

2. GOZ-Positionsreihenfolge
   - Durchschnittlicher Rang (lfdnr-Position) jeder GOZ-Nr. innerhalb eines KV
   - % der KVs in denen eine Position in den ersten 3 Stellen erscheint
   - Typische Sequenz-Paare: "X kommt fast immer vor Y"

Persistenz: Ergebnisse werden als JSON neben dem Katalog gespeichert.
Verwendung:
  from position_analyzer import (
      should_include_mko, sort_positions_by_rank,
      load_position_analysis, build_position_analysis,
  )
"""

import json
import datetime
from pathlib import Path
from collections import defaultdict

import db as db_module
from config import PROJEKTE_PFAD

ANALYSE_DATEI = Path(PROJEKTE_PFAD) / "position_analyse.json"

MKO_NUMMERN = frozenset({"8000", "8010", "8020", "8030", "8040",
                          "8050", "8060", "8070", "8080"})

# Mindestanteil für "gehört dazu" (0.0–1.0)
MKO_SCHWELLWERT_DEFAULT = 0.55   # >55% der MKO-KVs enthalten diese Position


# ─────────────────────────────────────────────────────────────────────────────
# DB-Abfragen
# ─────────────────────────────────────────────────────────────────────────────

def _query_mko_analysis(limit_kvs: int = 1000) -> dict:
    """
    Analysiert MKO-Häufigkeit und Co-Occurrences in historischen KVs.

    Rückgabe:
      {
        "total_kvs":    int,
        "mko_kvs":      int,
        "mko_pct":      float,
        "co_occurrence": [
            {"goz_nr": "2210", "kvs": 312, "pct": 0.74, "avg_rank_diff": -2.1},
            ...
        ],
        "non_mko_top": [{"goz_nr": "...", "kvs": int, "pct": float}, ...]
      }
    """
    with db_module.get_connection() as conn:
        with conn.cursor() as cur:

            # 1) Gesamt-KV-Anzahl (neueste N)
            cur.execute(
                "SELECT COUNT(*) FROM public.kv_main"
            )
            total_kvs = cur.fetchone()[0]

            # 2) KV-IDs der neuesten limit_kvs KVs
            cur.execute(
                "SELECT kvid FROM public.kv_main ORDER BY kvid DESC LIMIT %s",
                (limit_kvs,)
            )
            all_kv_ids = [r[0] for r in cur.fetchall()]
            if not all_kv_ids:
                return {}

            ph = ",".join(["%s"] * len(all_kv_ids))

            # 3) KVs die MKO enthalten
            cur.execute(
                f"""SELECT DISTINCT km.kvid
                    FROM public.kv_daten kd
                    JOIN public.kv_main km ON km.solid = kd.kvmainid
                    WHERE km.kvid IN ({ph})
                      AND kd.nummer = ANY(%s)""",
                all_kv_ids + [list(MKO_NUMMERN)]
            )
            mko_kv_ids = [r[0] for r in cur.fetchall()]
            n_mko = len(mko_kv_ids)

            if n_mko == 0:
                return {
                    "total_kvs": total_kvs,
                    "analysiert_kvs": len(all_kv_ids),
                    "mko_kvs": 0,
                    "mko_pct": 0.0,
                    "co_occurrence": [],
                    "non_mko_top": [],
                }

            mko_ph = ",".join(["%s"] * len(mko_kv_ids))

            # 4) Co-Occurrence: GOZ-Positionen in MKO-KVs (ohne MKO selbst)
            cur.execute(
                f"""SELECT
                        kd.nummer,
                        COUNT(DISTINCT km.kvid)              AS kvs_mit_position,
                        ROUND(AVG(kd.lfdnr::numeric), 2)     AS avg_rank,
                        MAX(kd.bezeichnung)                  AS text
                    FROM public.kv_daten kd
                    JOIN public.kv_main km ON km.solid = kd.kvmainid
                    WHERE km.kvid IN ({mko_ph})
                      AND kd.nummer NOT IN ('8000','8010','8020','8030',
                                            '8040','8050','8060','8070','8080')
                      AND kd.nummer IS NOT NULL AND kd.nummer != ''
                    GROUP BY kd.nummer
                    ORDER BY kvs_mit_position DESC
                    LIMIT 40""",
                mko_kv_ids
            )
            co_rows = cur.fetchall()

            # 5) Non-MKO KVs: Top-Positionen zum Vergleich
            non_mko_ids = [k for k in all_kv_ids if k not in set(mko_kv_ids)]
            non_mko_top = []
            if non_mko_ids:
                nm_ph = ",".join(["%s"] * len(non_mko_ids[:300]))
                cur.execute(
                    f"""SELECT kd.nummer, COUNT(DISTINCT km.kvid) AS kvs
                        FROM public.kv_daten kd
                        JOIN public.kv_main km ON km.solid = kd.kvmainid
                        WHERE km.kvid IN ({nm_ph})
                          AND kd.nummer IS NOT NULL AND kd.nummer != ''
                        GROUP BY kd.nummer
                        ORDER BY kvs DESC LIMIT 20""",
                    non_mko_ids[:300]
                )
                non_mko_top = [
                    {"goz_nr": r[0], "kvs": r[1],
                     "pct": round(r[1] / max(len(non_mko_ids[:300]), 1), 3)}
                    for r in cur.fetchall()
                ]

            # 6) Durchschnittlicher MKO-Rang (für Vergleich mit anderen Positionen)
            cur.execute(
                f"""SELECT ROUND(AVG(kd.lfdnr::numeric), 2)
                    FROM public.kv_daten kd
                    JOIN public.kv_main km ON km.solid = kd.kvmainid
                    WHERE km.kvid IN ({mko_ph})
                      AND kd.nummer = ANY(%s)""",
                mko_kv_ids + [list(MKO_NUMMERN)]
            )
            mko_avg_rank = float(cur.fetchone()[0] or 0)

    co_occurrence = [
        {
            "goz_nr":    r[0],
            "kvs":       r[1],
            "pct":       round(r[1] / n_mko, 3),
            "avg_rank":  float(r[2] or 0),
            "text":      (r[3] or "")[:60],
            # Negativ = erscheint typisch VOR MKO; Positiv = danach
            "rank_diff_to_mko": round(float(r[2] or 0) - mko_avg_rank, 2),
        }
        for r in co_rows
    ]

    return {
        "total_kvs":       total_kvs,
        "analysiert_kvs":  len(all_kv_ids),
        "mko_kvs":         n_mko,
        "mko_pct":         round(n_mko / len(all_kv_ids), 3),
        "mko_avg_rank":    mko_avg_rank,
        "co_occurrence":   co_occurrence,
        "non_mko_top":     non_mko_top,
    }


def _query_position_ordering(limit_kvs: int = 800) -> dict:
    """
    Analysiert die typische Reihenfolge (lfdnr) von GOZ-Positionen.

    Rückgabe:
      {
        "positions": {
          "goz_nr": {
            "avg_rank":      float,   # mittlere lfdnr-Position im KV
            "median_rank":   float,
            "pct_top3":      float,   # % der KVs wo dieser Code in Top 3 erscheint
            "pct_top5":      float,
            "pct_first":     float,   # % wo dieser Code an Stelle 1 erscheint
            "kvs":           int,
            "text":          str,
          },
          ...
        },
        "sequence_pairs": [          # häufige Vor-Nach-Paare
          {"before": "Ä1", "after": "2210", "pct": 0.82, "kvs": 234},
          ...
        ]
      }
    """
    with db_module.get_connection() as conn:
        with conn.cursor() as cur:

            # Neueste N KVs
            cur.execute(
                "SELECT kvid FROM public.kv_main ORDER BY kvid DESC LIMIT %s",
                (limit_kvs,)
            )
            kv_ids = [r[0] for r in cur.fetchall()]
            if not kv_ids:
                return {}

            ph = ",".join(["%s"] * len(kv_ids))

            # Rang-Statistiken je GOZ-Nr.
            cur.execute(
                f"""WITH ranked AS (
                    SELECT
                        kd.nummer,
                        kd.lfdnr,
                        MAX(kd.bezeichnung)  AS text,
                        km.kvid,
                        ROW_NUMBER() OVER (
                            PARTITION BY km.kvid ORDER BY kd.lfdnr
                        ) AS pos_in_kv,
                        COUNT(*) OVER (PARTITION BY km.kvid) AS total_in_kv
                    FROM public.kv_daten kd
                    JOIN public.kv_main km ON km.solid = kd.kvmainid
                    WHERE km.kvid IN ({ph})
                      AND kd.nummer IS NOT NULL AND kd.nummer != ''
                    GROUP BY kd.nummer, kd.lfdnr, km.kvid
                )
                SELECT
                    nummer,
                    ROUND(AVG(lfdnr::numeric), 2)             AS avg_rank,
                    ROUND(PERCENTILE_CONT(0.5)
                          WITHIN GROUP (ORDER BY lfdnr), 2)   AS median_rank,
                    ROUND(AVG(CASE WHEN pos_in_kv <= 3 THEN 1.0 ELSE 0.0 END), 3)
                                                              AS pct_top3,
                    ROUND(AVG(CASE WHEN pos_in_kv <= 5 THEN 1.0 ELSE 0.0 END), 3)
                                                              AS pct_top5,
                    ROUND(AVG(CASE WHEN pos_in_kv = 1 THEN 1.0 ELSE 0.0 END), 3)
                                                              AS pct_first,
                    COUNT(DISTINCT kvid)                      AS kvs,
                    MAX(text)                                 AS text
                FROM ranked
                GROUP BY nummer
                HAVING COUNT(DISTINCT kvid) >= 10
                ORDER BY avg_rank""",
                kv_ids
            )
            pos_rows = cur.fetchall()

            # Sequenz-Paare: welche Position erscheint fast immer VOR welcher anderen?
            # Strategie: für jede Kombination (A, B) in einem KV prüfen ob A.lfdnr < B.lfdnr
            # Einschränkung auf Top-30 häufigste Positionen für Performance
            top30_nrs = [r[0] for r in pos_rows[:30]]
            sequence_pairs = []

            if len(top30_nrs) >= 2:
                t30_ph = ",".join(["%s"] * len(top30_nrs))
                cur.execute(
                    f"""WITH pairs AS (
                        SELECT
                            a.nummer AS before_nr,
                            b.nummer AS after_nr,
                            km.kvid
                        FROM public.kv_daten a
                        JOIN public.kv_daten b
                             ON a.kvmainid = b.kvmainid AND a.lfdnr < b.lfdnr
                        JOIN public.kv_main km ON km.solid = a.kvmainid
                        WHERE km.kvid IN ({ph})
                          AND a.nummer IN ({t30_ph})
                          AND b.nummer IN ({t30_ph})
                          AND a.nummer != b.nummer
                    )
                    SELECT
                        before_nr,
                        after_nr,
                        COUNT(DISTINCT kvid) AS kvs_ordered
                    FROM pairs
                    GROUP BY before_nr, after_nr
                    HAVING COUNT(DISTINCT kvid) >= 20
                    ORDER BY kvs_ordered DESC
                    LIMIT 60""",
                    kv_ids + top30_nrs + top30_nrs
                )
                pair_rows = cur.fetchall()

                # Normalisieren: pct = ordered / (ordered + reversed)
                pair_dict: dict[tuple, int] = {
                    (r[0], r[1]): r[2] for r in pair_rows
                }
                seen: set = set()
                for (a, b), cnt_ab in pair_dict.items():
                    if (b, a) in seen or (a, b) in seen:
                        continue
                    cnt_ba = pair_dict.get((b, a), 0)
                    total = cnt_ab + cnt_ba
                    if total < 20:
                        continue
                    pct = round(cnt_ab / total, 3)
                    if pct >= 0.70:  # A kommt in ≥70% der Fälle vor B
                        sequence_pairs.append({
                            "before":  a,
                            "after":   b,
                            "kvs":     cnt_ab,
                            "total":   total,
                            "pct":     pct,
                        })
                    seen.add((a, b))

                sequence_pairs.sort(key=lambda x: x["pct"], reverse=True)

    positions = {
        r[0]: {
            "avg_rank":    float(r[1] or 0),
            "median_rank": float(r[2] or 0),
            "pct_top3":    float(r[3] or 0),
            "pct_top5":    float(r[4] or 0),
            "pct_first":   float(r[5] or 0),
            "kvs":         r[6],
            "text":        (r[7] or "")[:60],
        }
        for r in pos_rows
    }

    return {
        "positions":      positions,
        "sequence_pairs": sequence_pairs[:40],
    }


# ─────────────────────────────────────────────────────────────────────────────
# MKO-Cluster: Schwellwert-Entscheidung
# ─────────────────────────────────────────────────────────────────────────────

def _build_mko_clusters(mko_data: dict, schwellwert: float = MKO_SCHWELLWERT_DEFAULT) -> dict:
    """
    Leitet aus den Co-Occurrence-Daten ab bei welchen GOZ-Nummern
    MKO typischerweise dazugehört.

    Rückgabe: {
      "mko_pct_gesamt": 0.72,
      "schwellwert": 0.55,
      "immer_mit_mko": ["2210", "5190a", ...],   # >schwellwert in MKO-KVs
      "nie_mit_mko":   ["3000", "4010", ...],    # in Non-MKO-Top-Positionen
      "mko_trigger_nrs": ["2210", "2200i", ...], # Subset: wenn einer davon → MKO
    }
    """
    co = mko_data.get("co_occurrence", [])
    non_top = {r["goz_nr"] for r in mko_data.get("non_mko_top", [])}

    immer_mit = [r["goz_nr"] for r in co if r["pct"] >= schwellwert]
    nie_mit   = [r["goz_nr"] for r in mko_data.get("non_mko_top", [])
                 if r["goz_nr"] not in {c["goz_nr"] for c in co if c["pct"] >= 0.3}]

    # Trigger: prothetische Hauptpositionen die stark mit MKO korrelieren
    prothetik_nrs = {"2210", "2200i", "2190", "2200", "2180", "5190a", "2197", "2120z"}
    mko_trigger = [nr for nr in immer_mit if nr in prothetik_nrs]

    return {
        "mko_pct_gesamt":  mko_data.get("mko_pct", 0),
        "schwellwert":     schwellwert,
        "immer_mit_mko":   immer_mit,
        "nie_mit_mko":     nie_mit[:15],
        "mko_trigger_nrs": mko_trigger,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Build & Persist
# ─────────────────────────────────────────────────────────────────────────────

def build_position_analysis(
    limit_kvs: int = 800,
    mko_schwellwert: float = MKO_SCHWELLWERT_DEFAULT,
    status_callback=None,
) -> dict:
    """Führt alle Analysen durch und gibt das Ergebnis zurück (noch nicht gespeichert)."""

    def _cb(msg: str):
        if status_callback:
            status_callback(msg)

    _cb("🔍 Analysiere MKO-Häufigkeit und Co-Occurrences...")
    mko_data = _query_mko_analysis(limit_kvs=limit_kvs)

    _cb("📊 Berechne MKO-Cluster und Schwellwerte...")
    mko_clusters = _build_mko_clusters(mko_data, schwellwert=mko_schwellwert)

    _cb("📐 Analysiere Positionsreihenfolge (lfdnr)...")
    ordering_data = _query_position_ordering(limit_kvs=limit_kvs)

    result = {
        "erstellt_am":    datetime.datetime.now().isoformat(),
        "limit_kvs":      limit_kvs,
        "mko_schwellwert": mko_schwellwert,
        "mko":            mko_data,
        "mko_cluster":    mko_clusters,
        "ordering":       ordering_data,
    }

    _cb(f"✅ Analyse abgeschlossen – "
        f"{mko_data.get('analysiert_kvs',0)} KVs, "
        f"MKO in {mko_data.get('mko_pct',0)*100:.1f}% der KVs, "
        f"{len(ordering_data.get('positions',{}))} GOZ-Nummern, "
        f"{len(ordering_data.get('sequence_pairs',[]))} Sequenz-Paare")

    return result


def save_position_analysis(data: dict) -> Path:
    ANALYSE_DATEI.parent.mkdir(parents=True, exist_ok=True)
    with open(ANALYSE_DATEI, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return ANALYSE_DATEI


def load_position_analysis() -> dict | None:
    if not ANALYSE_DATEI.exists():
        return None
    try:
        with open(ANALYSE_DATEI, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def analyse_info() -> dict:
    d = load_position_analysis()
    if not d:
        return {"vorhanden": False}
    return {
        "vorhanden":    True,
        "erstellt_am":  d.get("erstellt_am", ""),
        "kvs":          d.get("mko", {}).get("analysiert_kvs", 0),
        "mko_pct":      d.get("mko", {}).get("mko_pct", 0),
        "n_positionen": len(d.get("ordering", {}).get("positions", {})),
        "n_paare":      len(d.get("ordering", {}).get("sequence_pairs", [])),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Verwendungs-API (für hkp_agents.py und katalog_builder.py)
# ─────────────────────────────────────────────────────────────────────────────

def should_include_mko(
    proposed_goz_nrs: list[str],
    analyse: dict | None = None,
    schwellwert: float | None = None,
) -> tuple[bool, str]:
    """
    Entscheidet ob MKO in diesem Fall dazugehört.

    Args:
        proposed_goz_nrs: GOZ-Nummern die der Agent bereits vorschlägt
        analyse: geladene position_analysis (None → wird geladen)
        schwellwert: Override für MKO_SCHWELLWERT_DEFAULT

    Returns:
        (include: bool, reason: str)
    """
    if analyse is None:
        analyse = load_position_analysis()

    if not analyse:
        # Kein Analyseergebnis → Fallback: immer einschließen (altes Verhalten)
        return True, "Kein Analyseergebnis – Fallback: MKO immer einschließen"

    cluster = analyse.get("mko_cluster", {})
    sw = schwellwert or cluster.get("schwellwert", MKO_SCHWELLWERT_DEFAULT)
    trigger_nrs = set(cluster.get("mko_trigger_nrs", []))
    immer_mit   = set(cluster.get("immer_mit_mko", []))

    proposed_set = set(proposed_goz_nrs)

    # Prüfe: ist mindestens eine Trigger-Position dabei?
    matched = proposed_set & trigger_nrs
    if matched:
        return True, f"MKO einschließen: Trigger-Positionen gefunden ({', '.join(sorted(matched))})"

    # Prüfe: ist mindestens eine "immer mit MKO"-Position dabei?
    matched2 = proposed_set & immer_mit
    if matched2:
        pct_examples = [
            f"{nr} {round(next((r['pct'] for r in analyse['mko']['co_occurrence'] if r['goz_nr']==nr), 0)*100):.0f}%"
            for nr in list(matched2)[:3]
        ]
        return True, f"MKO einschließen: Co-Occurrence über Schwellwert ({', '.join(pct_examples)})"

    mko_gesamt = cluster.get("mko_pct_gesamt", 0)
    return False, f"MKO weglassen: Keine Trigger-Position gefunden (MKO gesamt {mko_gesamt*100:.0f}%)"


def sort_positions_by_rank(
    positions: list[dict],
    analyse: dict | None = None,
) -> list[dict]:
    """
    Sortiert GOZ-Positionen nach typischer lfdnr-Reihenfolge aus der DB-Analyse.
    Positionen ohne Analysedaten bleiben am Ende.

    positions: Liste von {"goz_nr": str, ...}
    Gibt sortierte Liste zurück (unverändertes Original wenn keine Analysedaten).
    """
    if analyse is None:
        analyse = load_position_analysis()

    if not analyse:
        return positions

    rank_map: dict[str, float] = {
        nr: info["avg_rank"]
        for nr, info in analyse.get("ordering", {}).get("positions", {}).items()
    }

    def _sort_key(pos: dict) -> float:
        nr = pos.get("goz_nr", "")
        # Exact match
        if nr in rank_map:
            return rank_map[nr]
        # Suffix-tolerant: "5190a" → check "5190"
        base = nr.rstrip("abcdefghijklmnopqrstuvwxyz")
        if base in rank_map:
            return rank_map[base]
        return 999.0   # Unbekannte Positionen ans Ende

    return sorted(positions, key=_sort_key)


def get_sequence_hints(
    goz_nr: str,
    analyse: dict | None = None,
) -> dict:
    """
    Gibt Sequenz-Hinweise für eine GOZ-Nr. zurück:
    - typisch_vor: [{"goz_nr": "...", "pct": 0.85}]  – was kommt typisch davor
    - typisch_nach: [...]                              – was kommt danach
    """
    if analyse is None:
        analyse = load_position_analysis()
    if not analyse:
        return {"typisch_vor": [], "typisch_nach": []}

    pairs = analyse.get("ordering", {}).get("sequence_pairs", [])
    vor  = [{"goz_nr": p["before"], "pct": p["pct"]}
            for p in pairs if p["after"]  == goz_nr]
    nach = [{"goz_nr": p["after"],  "pct": p["pct"]}
            for p in pairs if p["before"] == goz_nr]

    return {"typisch_vor": vor[:5], "typisch_nach": nach[:5]}
