"""
DentalHKP – KI-gestützte GOZ-Planung
Streamlit-Anwendung
"""
import streamlit as st
import json
import glob
from pathlib import Path
from collections import defaultdict
import db as db_module
from tooth_decoder import extract_planned_teeth, find_goz_gap
from hkp_agents import run_hkp_pipeline
from projekt_manager import build_hkp_projekt, save_projekt, generate_word, list_hkp_projekte
from text_parser import (
    parse_treatment_text, validate_parsed_teeth,
    apply_correction, to_gap_tooth,
    CONFIDENCE_OK, CONFIDENCE_UNCLEAR, CONFIDENCE_CONFLICT,
)
from katalog_builder import BEHANDLUNGEN_CONFIG as _BEHANDLUNGEN_CFG

# ─────────────────────────────────────────────────────────────────────────────
# Seitenkonfiguration
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="DentalHKP",
    page_icon="🦷",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stButton > button { width:100%; }
    .goz-gruen { background:#d4edda; color:#155724; border-left:4px solid #28a745; padding:4px 8px; margin:2px 0; border-radius:3px; }
    .goz-gelb  { background:#fff3cd; color:#856404; border-left:4px solid #ffc107; padding:4px 8px; margin:2px 0; border-radius:3px; }
    .goz-none  { background:#f8f9fa; color:#495057; border-left:4px solid #6c757d; padding:4px 8px; margin:2px 0; border-radius:3px; }
    .goz-manual{ background:#e8d5f5; color:#4a235a; border-left:4px solid #8e44ad; padding:4px 8px; margin:2px 0; border-radius:3px; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: GOZ-Status-Farbe berechnen
# ─────────────────────────────────────────────────────────────────────────────

def _goz_status(goz_nr: str, existing_nrs: set, ai_green: set, ai_yellow: set) -> tuple[str, str]:
    """
    Gibt (status_label, hex_color) zurück:
      grau   = bereits im HKP
      gruen  = KI: Pflicht
      gelb   = KI: Empfohlen
      rot    = unwahrscheinlich/nicht empfohlen
    """
    if goz_nr in existing_nrs:
        return "✔ Im HKP", "#e8e8e8"
    if goz_nr in ai_green:
        return "🟢 Pflicht",  "#d4edda"
    if goz_nr in ai_yellow:
        return "🟡 Empf.",    "#fff3cd"
    return "🔴 Selten", "#f8d7da"

# ─────────────────────────────────────────────────────────────────────────────
# Session State initialisieren
# ─────────────────────────────────────────────────────────────────────────────

for key, default in [
    ("selected_patient", None),
    ("selected_kv_solid", None),
    ("kv_details", None),
    ("tooth_plan", None),
    ("gap_teeth", None),
    ("agent_result", None),
    ("agent_log", []),
    ("saved_ordner", None),
    ("manual_goz", []),        # Manuell hinzugefügte GOZ-Positionen
    ("kv_material", []),       # Material-Daten
    ("kv_labor", {}),          # Labor-Daten
    ("karies_befund", {}),     # Kariesdaten aus befundze
    ("text_parser_result", None),   # Ergebnis der Texteingabe-Analyse
    ("text_parser_corrections", {}), # Nutzer-Korrekturen {idx: {...}}
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: Patientensuche + KV-Auswahl
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🦷 DentalHKP")
    st.caption("KI-gestützte GOZ-Planung")
    st.divider()

    # ── Patientensuche ──────────────────────────────────────────────────────
    st.subheader("Patientensuche")
    patient_query = st.text_input(
        "Name oder Vorname eingeben",
        placeholder="z.B. Christ oder Volker",
        key="patient_search_input",
    )

    if patient_query and len(patient_query) >= 2:
        with st.spinner("Suche..."):
            patients = db_module.search_patients(patient_query)
        if patients:
            options = {f"{p['name']}, {p['vorname']} (*{p['geburtsdatum']})": p for p in patients}
            selected_label = st.selectbox("Treffer", options=list(options.keys()), key="patient_select")
            if selected_label:
                selected_patient = options[selected_label]
                if st.session_state.selected_patient != selected_patient:
                    st.session_state.selected_patient = selected_patient
                    st.session_state.selected_kv_solid = None
                    st.session_state.kv_details = None
                    st.session_state.tooth_plan = None
                    st.session_state.gap_teeth = None
                    st.session_state.agent_result = None
                    st.session_state.manual_goz = []
                    st.session_state.kv_material = []
                    st.session_state.kv_labor = {}
        else:
            st.info("Keine Patienten gefunden.")

    # ── KV-Auswahl ──────────────────────────────────────────────────────────
    if st.session_state.selected_patient:
        pat = st.session_state.selected_patient
        st.divider()
        st.subheader("KV-Einträge")
        st.caption(f"Patient: **{pat['name']}, {pat['vorname']}**")

        with st.spinner("Lade KVs..."):
            kvs = db_module.get_recent_kvs(patid=pat["solid"], limit=20)

        if kvs:
            kv_options = {}
            for kv in kvs:
                label = f"{kv['datum']}  |  {kv['kurztext'] or '–'}  |  #{kv['solid']}"
                kv_options[label] = kv["solid"]

            selected_kv_label = st.selectbox("HKP auswählen", options=list(kv_options.keys()), key="kv_select")

            if selected_kv_label:
                kv_solid = kv_options[selected_kv_label]
                if st.session_state.selected_kv_solid != kv_solid:
                    st.session_state.selected_kv_solid = kv_solid
                    st.session_state.kv_details = None
                    st.session_state.tooth_plan = None
                    st.session_state.gap_teeth = None
                    st.session_state.agent_result = None
                    st.session_state.manual_goz = []

                if st.button("📋 KV laden & analysieren", type="primary"):
                    with st.spinner("Analysiere KV..."):
                        st.session_state.kv_details = db_module.get_kv_details(kv_solid)
                        st.session_state.tooth_plan = db_module.get_tooth_plan_for_kv(kv_solid)
                        st.session_state.kv_material = db_module.get_kv_material(kv_solid)
                        st.session_state.kv_labor = db_module.get_kv_labor_summary(kv_solid)
                        _caries_raw = db_module.get_befundze_for_kv(kv_solid)
                        st.session_state.karies_befund = _caries_raw

                    if st.session_state.tooth_plan:
                        planned = extract_planned_teeth(st.session_state.tooth_plan)
                        positionen = st.session_state.kv_details.get("positionen", []) if st.session_state.kv_details else []
                        _gap = find_goz_gap(planned, positionen)
                    else:
                        _gap = []

                    # Karies-Zähne aus befundze ergänzen (falls nicht schon geplant)
                    _planned_zaehne = {t["zahn"] for t in (extract_planned_teeth(st.session_state.tooth_plan) if st.session_state.tooth_plan else [])}
                    _gap_zaehne = {t["zahn"] for t in _gap}
                    for _zn, _ci in _caries_raw.items():
                        if _zn not in _planned_zaehne and _zn not in _gap_zaehne:
                            _gap.append(db_module.caries_to_treatment(_zn, _ci))
                    st.session_state.gap_teeth = _gap

                    st.session_state.agent_result = None
                    st.session_state.agent_log = []
                    st.session_state.manual_goz = []
                    st.rerun()
        else:
            st.info("Keine KVs gefunden.")

    # ── Neueste KVs (ohne Patientenauswahl) ─────────────────────────────────
    st.divider()
    if st.button("🕐 Neueste KVs (alle Patienten)"):
        with st.spinner("Lade..."):
            recent = db_module.get_recent_kvs(limit=15)
        if recent:
            kv_options = {}
            for kv in recent:
                label = f"{kv['datum']}  |  {kv['patient_name']}  |  {kv['kurztext'] or '–'}"
                kv_options[label] = kv["solid"]
            st.session_state["recent_kvs_list"] = kv_options

    # ── Stücklisten-Katalog ─────────────────────────────────────────────────
    st.divider()
    from katalog_builder import katalog_info, build_katalog, save_katalog, BEHANDLUNGEN_CONFIG
    from config import USE_KATALOG as _USE_KATALOG

    _ki = katalog_info()
    if _ki["vorhanden"]:
        _kat_datum = _ki["erstellt_am"][:10]
        _kat_n     = _ki["n_behandlungstypen"]
        _kat_kvs   = _ki["kvs_gesamt"]
        st.success(f"📚 Katalog aktiv · {_kat_n} Typen · {_kat_kvs} KVs · {_kat_datum}")
        if not _USE_KATALOG:
            st.warning("⚠️ USE_KATALOG=false – Katalog vorhanden aber deaktiviert")
    else:
        st.warning("📚 Kein Stücklisten-Katalog vorhanden")

    with st.expander("📚 Stücklisten-Katalog verwalten", expanded=False):
        # Einzelne Typen auswählen
        alle_typen = list(BEHANDLUNGEN_CONFIG.keys())
        typ_labels = {k: BEHANDLUNGEN_CONFIG[k]["bezeichnung"] for k in alle_typen}
        ausgewaehlte = st.multiselect(
            "Behandlungstypen aktualisieren:",
            options=alle_typen,
            default=alle_typen,
            format_func=lambda k: typ_labels.get(k, k),
            key="katalog_typen_select",
        )
        if st.button("🔄 Katalog jetzt aufbauen", type="primary",
                     disabled=not ausgewaehlte, key="btn_build_katalog"):
            _kat_log = st.empty()
            _kat_log.info("⏳ Starte Chefarzt-Analyse...")
            _kat_msgs = []

            def _kat_cb(msg: str):
                _kat_msgs.append(msg)
                _kat_log.info(msg)

            with st.spinner("Chefarzt-Agent analysiert historische KVs..."):
                try:
                    _neuer_katalog = build_katalog(
                        behandlung_keys=ausgewaehlte,
                        status_callback=_kat_cb,
                    )
                    save_katalog(_neuer_katalog)
                    _kat_log.success(
                        f"✅ Katalog gespeichert – "
                        f"{len(_neuer_katalog['behandlungen'])} Behandlungstypen"
                    )
                    st.rerun()
                except Exception as _e:
                    _kat_log.error(f"❌ Fehler: {_e}")
                    st.exception(_e)

        # Katalog-Vorschau (via session state – übersteht Streamlit-Rerenders)
        if _ki["vorhanden"]:
            _col_prev, _col_del = st.columns([2, 1])
            with _col_prev:
                if st.button("👁 Katalog anzeigen", key="btn_katalog_preview"):
                    from katalog_builder import load_katalog as _lk
                    st.session_state["_katalog_preview"] = _lk()
            with _col_del:
                if st.button("🗑 Zurücksetzen", key="btn_katalog_clear",
                             help="Katalog-Vorschau ausblenden"):
                    st.session_state.pop("_katalog_preview", None)

        _kd_preview = st.session_state.get("_katalog_preview")
        if _kd_preview:
            _kat_icon_map = {"pflicht": "🟢", "empfohlen": "🟡",
                             "session": "🔁", "optional": "⬜"}
            for _bkey, _beintrag in _kd_preview.get("behandlungen", {}).items():
                _n_ok  = len(_beintrag.get("positionen", [])) if not _beintrag.get("hat_varianten") else sum(
                    len(v.get("positionen", [])) for v in _beintrag.get("varianten", {}).values()
                )
                _score = _beintrag.get("qualitaet_score", 0)
                _has_err = _beintrag.get("chefarzt_hinweise", "").startswith("⚠️")
                _title = (f"{'⚠️' if _has_err else '✅'} {_beintrag['bezeichnung']} "
                          f"({_n_ok} Pos. | {_beintrag.get('kvs_analysiert',0)} KVs"
                          f"{'' if _beintrag.get('hat_varianten') else f' | Q:{_score}/100'})")
                with st.expander(_title, expanded=False):
                    if _beintrag.get("hat_varianten"):
                        for _vn, _ve in _beintrag.get("varianten", {}).items():
                            _vpos = _ve.get("positionen", [])
                            st.markdown(f"**{_vn}** – GOZ `{_ve.get('haupt_goz','')}` | "
                                        f"{len(_vpos)} Pos. | {_ve.get('kvs_analysiert',0)} KVs")
                            for _p in _vpos:
                                _ic = _kat_icon_map.get(_p.get("kategorie", "optional"), "⬜")
                                st.caption(f"  {_ic} **{_p['goz_nr']}** {_p.get('text','')[:45]} "
                                           f"| ×{_p.get('avg_faktor',2.3):.1f} "
                                           f"| {_p.get('haeufigkeit_pct',0):.0f}%")
                            if _ve.get("chefarzt_hinweise"):
                                st.info(_ve["chefarzt_hinweise"][:250])
                    else:
                        for _p in _beintrag.get("positionen", []):
                            _ic = _kat_icon_map.get(_p.get("kategorie", "optional"), "⬜")
                            st.caption(f"  {_ic} **{_p['goz_nr']}** {_p.get('text','')[:45]} "
                                       f"| ×{_p.get('avg_faktor',2.3):.1f} "
                                       f"| {_p.get('haeufigkeit_pct',0):.0f}% "
                                       f"| {_p.get('begruendung','')[:40]}")
                        hint = _beintrag.get("chefarzt_hinweise", "")
                        if hint:
                            (st.warning if hint.startswith("⚠️") else st.info)(hint[:300])

    # ── Gespeicherte Projekte ────────────────────────────────────────────────
    st.divider()
    if st.button("📂 HKP-Projekte laden"):
        projekte = list_hkp_projekte(limit=15)
        st.session_state["gespeicherte_projekte"] = projekte if projekte else []

    if st.session_state.get("gespeicherte_projekte") is not None:
        projekte = st.session_state["gespeicherte_projekte"]
        if not projekte:
            st.info("Noch keine Projekte gespeichert.")
        else:
            st.caption(f"**{len(projekte)} Projekte:**")
            for p_idx, p in enumerate(projekte):
                ordner = p.get("ordner", "")
                pat    = p.get("patient", "?")
                datum  = p.get("datum", "")
                kv_id  = p.get("kv_id", "?")
                gap_n  = p.get("gap_anzahl", "?")

                with st.expander(f"📄 {datum} {pat}", expanded=False):
                    st.caption(f"KV #{kv_id} | {gap_n} GOZ-Lücken | Hon: {p.get('honorar',0):.0f}€")

                    json_path = Path(ordner) / "projekt_daten.json"

                    # ── In App laden (Bearbeitung fortsetzen) ────────────────
                    if json_path.exists():
                        if st.button("📂 In App laden & bearbeiten",
                                     key=f"load_proj_{p_idx}", type="primary"):
                            with open(json_path, encoding="utf-8") as _f:
                                _proj = json.load(_f)

                            _kv   = _proj.get("kv", {})
                            _pat  = _proj.get("patient", {})
                            _ae   = _proj.get("agent_ergebnisse", {})
                            _kva  = _proj.get("kostenvoranschlag", {})

                            # Session State rekonstruieren
                            st.session_state.kv_details = {
                                "solid":        _kv.get("id"),
                                "kurztext":     _kv.get("kurztext", ""),
                                "datum":        _kv.get("datum", ""),
                                "kvstatus":     _kv.get("status"),
                                "honorar":      _kv.get("honorar", 0),
                                "material":     _kv.get("material", 0),
                                "labor":        _kv.get("labor", 0),
                                "patient_name": f"{_pat.get('name','')}, {_pat.get('vorname','')}",
                                "patid":        _pat.get("id"),
                                "positionen":   _proj.get("bestehende_goz", []),
                            }
                            st.session_state.selected_patient = {
                                "solid":        _pat.get("id"),
                                "name":         _pat.get("name", ""),
                                "vorname":      _pat.get("vorname", ""),
                                "geburtsdatum": _pat.get("geburtsdatum", ""),
                            }
                            st.session_state.selected_kv_solid = _kv.get("id")
                            st.session_state.gap_teeth    = _proj.get("gap_teeth", [])
                            st.session_state.tooth_plan   = None
                            st.session_state.manual_goz   = _proj.get("manual_goz", [])
                            st.session_state.kv_material  = _proj.get("material", [])
                            st.session_state.kv_labor     = _proj.get("labor", {})
                            # Karies-Befund: aus gap_teeth rekonstruieren (source=karies_befundze)
                            _karies_reload = {}
                            for _gt in _proj.get("gap_teeth", []):
                                if _gt.get("source") == "karies_befundze":
                                    _karies_reload[_gt["zahn"]] = {
                                        "wert":         _gt.get("karies_wert", 0),
                                        "flaechen":     _gt.get("karies_flaechen", 1),
                                        "flaechen_text": "",
                                    }
                            st.session_state.karies_befund = _karies_reload
                            st.session_state.agent_result = {
                                "archiv":         _ae.get("archiv_analyse", ""),
                                "goz_structured": _ae.get("goz_vorschlaege", {}),
                                "qualitaet":      _ae.get("qualitaetspruefung", ""),
                            }
                            st.session_state.agent_log    = []
                            st.session_state.saved_ordner = str(Path(ordner))

                            # Checkbox-Zustände aus kostenvoranschlag wiederherstellen
                            for _k in list(st.session_state.keys()):
                                if _k.startswith("goz_cb_"):
                                    del st.session_state[_k]
                            _sel_set = {
                                (str(_s["zahn"]), _s["goz_nr"])
                                for _s in _kva.get("positionen", [])
                            }
                            for _zd in _ae.get("goz_vorschlaege", {}).get("zaehne", []):
                                _zn = _zd.get("zahn", "?")
                                for _pi, _pos in enumerate(_zd.get("positionen", [])):
                                    _cbk = f"goz_cb_{_zn}_{_pos.get('goz_nr','x')}_{_pi}"
                                    st.session_state[_cbk] = (
                                        str(_zn), _pos.get("goz_nr", "")
                                    ) in _sel_set

                            st.rerun()

                    # ── Downloads ────────────────────────────────────────────
                    dl_c1, dl_c2 = st.columns(2)
                    with dl_c1:
                        if json_path.exists():
                            with open(json_path, encoding="utf-8") as f:
                                st.download_button(
                                    "⬇️ JSON",
                                    data=f.read().encode("utf-8"),
                                    file_name=f"HKP_KV{kv_id}.json",
                                    mime="application/json",
                                    key=f"dl_json_{p_idx}_{kv_id}",
                                )
                    with dl_c2:
                        word_files = glob.glob(str(Path(ordner) / "*.docx"))
                        if word_files:
                            with open(word_files[0], "rb") as f:
                                st.download_button(
                                    "⬇️ Word",
                                    data=f.read(),
                                    file_name=Path(word_files[0]).name,
                                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                    key=f"dl_word_{p_idx}_{kv_id}",
                                )
                    st.caption(f"📁 `{Path(ordner).name}`")


# ─────────────────────────────────────────────────────────────────────────────
# Willkommensseite
# ─────────────────────────────────────────────────────────────────────────────

if not st.session_state.selected_kv_solid:
    st.title("🦷 DentalHKP – KI-gestützte GOZ-Planung")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.info("**1. Patient suchen**\nNamen im Suchfeld links eingeben")
    with col2:
        st.info("**2. HKP auswählen**\nAus der Liste der letzten KV-Einträge")
    with col3:
        st.info("**3. Agenten starten**\nKI analysiert GOZ-Lücken und schlägt Positionen vor")

    if "recent_kvs_list" in st.session_state and st.session_state["recent_kvs_list"]:
        st.subheader("🕐 Neueste KV-Einträge")
        for label, solid in st.session_state["recent_kvs_list"].items():
            if st.button(label, key=f"quick_{solid}"):
                with st.spinner("Lade KV..."):
                    st.session_state.selected_kv_solid = solid
                    kv_details = db_module.get_kv_details(solid)
                    st.session_state.kv_details = kv_details
                    if kv_details:
                        st.session_state.selected_patient = db_module.get_patient_by_id(kv_details["patid"])
                    st.session_state.tooth_plan = db_module.get_tooth_plan_for_kv(solid)
                    st.session_state.kv_material = db_module.get_kv_material(solid)
                    st.session_state.kv_labor = db_module.get_kv_labor_summary(solid)
                    _caries_raw = db_module.get_befundze_for_kv(solid)
                    st.session_state.karies_befund = _caries_raw
                    if st.session_state.tooth_plan:
                        planned = extract_planned_teeth(st.session_state.tooth_plan)
                        positionen = kv_details.get("positionen", []) if kv_details else []
                        _gap = find_goz_gap(planned, positionen)
                    else:
                        _gap = []
                    _planned_zaehne = {t["zahn"] for t in (extract_planned_teeth(st.session_state.tooth_plan) if st.session_state.tooth_plan else [])}
                    _gap_zaehne = {t["zahn"] for t in _gap}
                    for _zn, _ci in _caries_raw.items():
                        if _zn not in _planned_zaehne and _zn not in _gap_zaehne:
                            _gap.append(db_module.caries_to_treatment(_zn, _ci))
                    st.session_state.gap_teeth = _gap
                    st.session_state.agent_result = None
                    st.session_state.agent_log = []
                    st.session_state.manual_goz = []
                st.rerun()
    st.stop()


# ─────────────────────────────────────────────────────────────────────────────
# KV ist geladen → Analyse-Ansicht
# ─────────────────────────────────────────────────────────────────────────────

kv = st.session_state.kv_details
tooth_plan = st.session_state.tooth_plan
gap = st.session_state.gap_teeth or []

# Alle geplanten Zähne (für Agent-Analyse auch bei leerem Gap)
_all_planned_teeth = extract_planned_teeth(tooth_plan) if tooth_plan else []

# Für Agenten: wenn keine Lücken → alle geplanten Zähne zur Vollständigkeitsprüfung
agent_teeth = gap if gap else [
    {**t, "has_goz": True, "analyse_modus": "vollstaendigkeit"}
    for t in _all_planned_teeth
]

# ── Header ──────────────────────────────────────────────────────────────────
if kv:
    col_h1, col_h2, col_h3, col_h4, col_h5 = st.columns([3, 2, 2, 2, 2])
    with col_h1:
        st.title(f"🦷 {kv.get('patient_name', 'Patient')}")
    with col_h2:
        st.metric("KV-ID", f"#{kv['solid']}")
    with col_h3:
        st.metric("Datum", kv.get("datum", "?"))
    with col_h4:
        st.metric("Honorar", f"{(kv.get('honorar') or 0):.2f} €")
    with col_h5:
        labor_val = (kv.get('labor') or 0)
        st.metric("Labor", f"{labor_val:.2f} €")

    if kv.get("kurztext"):
        st.caption(f"Bezeichnung: **{kv['kurztext']}**  |  Status: {kv.get('kvstatus', '?')}")

st.divider()

# ── 2-Spalten Layout ────────────────────────────────────────────────────────
col_left, col_right = st.columns([1, 1])

# ── LINKE SPALTE: Zahnstatus & Gap-Analyse ──────────────────────────────────
with col_left:
    st.subheader("📊 Grafische Zahnplanung (befund01pa)")

    if not tooth_plan:
        st.warning("Kein befund01pa-Eintrag gefunden. Wurde die KV grafisch bearbeitet?")
    else:
        all_planned = extract_planned_teeth(tooth_plan)
        if not all_planned:
            st.info("Keine neu geplanten Behandlungen in der Grafik erkannt.")
        else:
            positionen = kv.get("positionen", []) if kv else []
            zaehne_mit_goz = set()
            for pos in positionen:
                for fdi in db_module.bitmask_to_fdi(pos.get("zahn_bitmask") or 0):
                    zaehne_mit_goz.add(fdi)

            table_data = []
            for t in all_planned:
                has_goz = t["zahn"] in zaehne_mit_goz
                implant_icon = " 🔩" if t.get("is_implant") else ""
                table_data.append({
                    "Zahn": t["zahn"],
                    "Behandlung": t["treatment_name"] + implant_icon,
                    "GOZ-Basis": t.get("goz_basis") or "–",
                    "GOZ vorhanden": "✅ Ja" if has_goz else "❌ Fehlt",
                })
            st.dataframe(table_data, use_container_width=True, hide_index=True)

    # Kariesbefund aus befundze anzeigen
    _karies = st.session_state.get("karies_befund", {})
    if _karies:
        st.subheader("🔴 Kariesbefund (befundze)")
        karies_data = []
        for _zn, _ci in sorted(_karies.items()):
            karies_data.append({
                "Zahn": _zn,
                "Flächen": _ci["flaechen"],
                "Flächen-Code": _ci["flaechen_text"],
                "GOZ-Vorschlag": {1: "2180 (1-fl.)", 2: "2190 (2-fl.)"}.get(_ci["flaechen"], "2200 (3-fl.+)"),
            })
        st.dataframe(karies_data, use_container_width=True, hide_index=True)

    # Gap-Zusammenfassung
    st.subheader("⚠️ GOZ-Lücken (ohne Abrechnung)")
    _has_planned = tooth_plan is not None and bool(extract_planned_teeth(tooth_plan))
    if not gap:
        if _has_planned:
            st.success("Alle geplanten Zähne haben GOZ-Einträge. Keine Lücken!")
        else:
            st.info("Keine Daten vorhanden.")
    else:
        for t in gap:
            implant_badge = " 🔩 **Implantat**" if t.get("is_implant") else ""
            if t.get("source") == "karies_befundze":
                st.warning(
                    f"**Zahn {t['zahn']}** 🔴 Karies – {t['treatment_name']} "
                    f"| GOZ-Basis: **{t.get('goz_basis') or '?'}** + 2197 "
                    f"({t.get('karies_flaechen', '?')} Fläche(n))"
                )
            else:
                st.error(
                    f"**Zahn {t['zahn']}** – {t['treatment_name']}{implant_badge} "
                    f"(Code {t['code']}, GOZ-Basis: {t.get('goz_basis') or '?'})"
                )

# ── RECHTE SPALTE: Bestehende GOZ-Einträge ──────────────────────────────────
with col_right:
    st.subheader("📋 Vorhandene GOZ-Einträge")

    if kv and kv.get("positionen"):
        positionen = kv["positionen"]
        if not any(p.get("goz_nr") for p in positionen):
            st.info("Noch keine GOZ-Positionen erfasst.")
        else:
            by_zahn = defaultdict(list)
            for pos in positionen:
                if pos.get("goz_nr"):
                    zaehne = db_module.bitmask_to_fdi(pos.get("zahn_bitmask") or 0)
                    zahn_str = ", ".join(str(z) for z in zaehne) if zaehne else "Allg."
                    by_zahn[zahn_str].append(pos)

            for zahn_str, pos_list in sorted(by_zahn.items()):
                with st.expander(f"Zahn {zahn_str}", expanded=False):
                    for pos in pos_list:
                        betrag = pos.get("betrag") or 0
                        st.markdown(
                            f"- **{pos['goz_nr']}** – {(pos.get('goz_text') or '')[:70]} "
                            f"| ×{pos.get('faktor', '-')} | **{betrag:.2f} €**"
                        )
    else:
        st.info("Keine Positionen geladen.")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# Texteingabe-Kanal: Freie Behandlungsplanung
# ─────────────────────────────────────────────────────────────────────────────

_tp_expanded = tooth_plan is None  # Prominent wenn keine grafische Planung vorhanden

with st.expander(
    "✍️ Freie Texteingabe / Diktat – Behandlungsplanung",
    expanded=_tp_expanded,
):
    if tooth_plan is None:
        st.info(
            "Kein grafischer Behandlungsplan vorhanden. "
            "Gib die geplanten Behandlungen als Freitext ein – "
            "die KI erkennt Zahnummern und Behandlungstypen automatisch."
        )
    else:
        st.caption(
            "Grafische Planung vorhanden. Texteingabe als Ergänzung möglich "
            "– neue Zähne werden zur GOZ-Lückenliste hinzugefügt."
        )

    txt_input = st.text_area(
        "Behandlungsnotiz",
        placeholder=(
            "Beispiele:\n"
            "  Zahn 11, 21 Vollkeramikkrone\n"
            "  35 Inlay 2-flächig, 36 Implantat Keramik\n"
            "  VMK 14 und 24, Teleskop 44"
        ),
        height=100,
        key="txt_freitext_input",
    )

    _col_parse, _col_reset = st.columns([2, 1])
    with _col_parse:
        _btn_parse = st.button(
            "🔍 Analysieren",
            type="primary",
            disabled=not (txt_input and txt_input.strip()),
            key="btn_parse_text",
        )
    with _col_reset:
        if st.button("🗑 Zurücksetzen", key="btn_parse_reset"):
            st.session_state.text_parser_result     = None
            st.session_state.text_parser_corrections = {}
            st.rerun()

    if _btn_parse and txt_input.strip():
        _pat_name = ""
        if st.session_state.selected_patient:
            _p = st.session_state.selected_patient
            _pat_name = f"{_p.get('name', '')} {_p.get('vorname', '')}".strip()
        with st.spinner("KI analysiert Texteingabe..."):
            _parsed_raw = parse_treatment_text(txt_input.strip(), _pat_name)
            _kv_pos = (st.session_state.kv_details or {}).get("positionen", [])
            _validated = validate_parsed_teeth(
                _parsed_raw,
                st.session_state.tooth_plan,
                st.session_state.karies_befund,
                _kv_pos,
            )
        st.session_state.text_parser_result      = _validated
        st.session_state.text_parser_corrections = {}
        st.rerun()

    # ── Ergebnis-Tabelle mit Ampel + Korrektur-Widgets ────────────────────────
    _tp_result = st.session_state.get("text_parser_result")
    if _tp_result is not None:
        st.markdown("---")
        st.markdown("#### Erkannte Zähne / Behandlungen")

        _conf_icon = {
            CONFIDENCE_OK:       "🟢",
            CONFIDENCE_UNCLEAR:  "🟡",
            CONFIDENCE_CONFLICT: "🔴",
        }

        # Behandlungstyp-Optionen für Dropdown
        _kat_options     = list(_BEHANDLUNGEN_CFG.keys())
        _kat_labels      = {k: _BEHANDLUNGEN_CFG[k]["bezeichnung"] for k in _kat_options}
        _kat_label_list  = [_kat_labels[k] for k in _kat_options]

        _has_unresolved = False   # Gibt es noch ungeklärte Einträge?
        _corrections    = st.session_state.text_parser_corrections

        for _idx, _item in enumerate(_tp_result):
            # Ggf. Korrekturen aus session state einarbeiten (für Anzeige)
            if _idx in _corrections:
                _item = apply_correction(_item, **_corrections[_idx])

            _conf  = _item.get("confidence", CONFIDENCE_UNCLEAR)
            _icon  = _conf_icon.get(_conf, "⚪")
            _zahn  = _item.get("zahn")
            _name  = _item.get("treatment_name") or _item.get("behandlung_raw", "?")
            _impl  = " 🔩" if _item.get("is_implant") else ""
            _fl    = f" {_item['karies_flaechen']}-fl." if _item.get("karies_flaechen") else ""
            _goz   = _item.get("goz_basis") or "?"

            with st.container():
                _c1, _c2, _c3 = st.columns([0.5, 3, 3])
                with _c1:
                    st.markdown(f"### {_icon}")
                with _c2:
                    if _zahn:
                        st.markdown(f"**Zahn {_zahn}** – {_name}{_impl}{_fl}")
                    else:
                        st.markdown(f"**Zahn ?** – {_name}")
                    st.caption(f"GOZ-Basis: `{_goz}`  |  raw: _{_item.get('behandlung_raw','')}_")
                with _c3:
                    _hinweis = _item.get("hinweis", "")
                    _conflict = _item.get("conflict_detail", "")
                    if _conflict:
                        st.warning(f"⚡ {_conflict}")
                    elif _hinweis:
                        st.info(f"ℹ️ {_hinweis}")

                # Korrektur-Widgets für unclear/conflict
                if _conf in (CONFIDENCE_UNCLEAR, CONFIDENCE_CONFLICT):
                    _has_unresolved = True
                    _key_pfx = f"tpcorr_{_idx}"
                    _w1, _w2, _w3 = st.columns([1, 2, 1])

                    with _w1:
                        _new_zahn = st.number_input(
                            "Zahnummer",
                            min_value=11, max_value=48,
                            value=int(_zahn) if _zahn else 11,
                            step=1,
                            key=f"{_key_pfx}_zahn",
                        )
                    with _w2:
                        # Aktuellen Typ vorselektieren
                        _cur_key = _item.get("katalog_key")
                        _cur_idx = _kat_options.index(_cur_key) if _cur_key in _kat_options else 0
                        _sel_label = st.selectbox(
                            "Behandlungstyp",
                            options=_kat_label_list,
                            index=_cur_idx,
                            key=f"{_key_pfx}_typ",
                        )
                        _sel_key = _kat_options[_kat_label_list.index(_sel_label)]
                    with _w3:
                        _override = False
                        if _conf == CONFIDENCE_CONFLICT:
                            _override = st.checkbox(
                                "Konflikt OK",
                                value=False,
                                key=f"{_key_pfx}_override",
                                help="Konflikt bestätigen und trotzdem übernehmen",
                            )

                    # Karies-Flächen für Inlay
                    _fl_val = None
                    if _sel_key == "Inlay_Cerec":
                        _fl_val = st.selectbox(
                            "Flächen (Inlay)",
                            options=[1, 2, 3],
                            index=max(0, min(2, (_item.get("karies_flaechen") or 2) - 1)),
                            key=f"{_key_pfx}_flaechen",
                        )

                    if st.button("✔ Korrektur anwenden", key=f"{_key_pfx}_apply"):
                        _corr = {
                            "new_zahn":          int(_new_zahn),
                            "new_katalog_key":   _sel_key,
                        }
                        if _fl_val is not None:
                            _corr["new_karies_flaechen"] = _fl_val
                        if _override:
                            _corr["override_conflict"] = True
                        st.session_state.text_parser_corrections[_idx] = _corr
                        # Re-validate this item
                        _fixed = apply_correction(_item, **_corr)
                        _kv_pos2 = (st.session_state.kv_details or {}).get("positionen", [])
                        _rev = validate_parsed_teeth(
                            [_fixed],
                            st.session_state.tooth_plan,
                            st.session_state.karies_befund,
                            _kv_pos2,
                        )
                        st.session_state.text_parser_result[_idx] = _rev[0]
                        st.session_state.text_parser_corrections.pop(_idx, None)
                        st.rerun()

                st.markdown("---")

        # ── Zusammenfassung & Übernahme-Button ────────────────────────────────
        _ok_items = [
            _tp_result[i] for i in range(len(_tp_result))
            if _tp_result[i].get("confidence") == CONFIDENCE_OK
        ]
        _total = len(_tp_result)
        _ok_n  = len(_ok_items)

        st.markdown(
            f"**Ergebnis:** {_ok_n}/{_total} Einträge bereit  "
            f"{'✅' if _ok_n == _total else '⚠️ Bitte Korrekturen oben anwenden.'}"
        )

        if st.button(
            f"✅ {_ok_n} Zahn/Zähne in Planung übernehmen",
            type="primary",
            disabled=(_ok_n == 0),
            key="btn_text_uebernehmen",
        ):
            _existing_gap_zaehne = {t["zahn"] for t in (st.session_state.gap_teeth or [])}
            _added = 0
            for _item in _ok_items:
                if _item.get("zahn") and _item["zahn"] not in _existing_gap_zaehne:
                    _gt = to_gap_tooth(_item)
                    if st.session_state.gap_teeth is None:
                        st.session_state.gap_teeth = []
                    st.session_state.gap_teeth.append(_gt)
                    _existing_gap_zaehne.add(_item["zahn"])
                    _added += 1
            st.session_state.text_parser_result      = None
            st.session_state.text_parser_corrections = {}
            if _added > 0:
                st.success(f"✅ {_added} Zahn/Zähne zur Planung hinzugefügt.")
            else:
                st.info("Alle erkannten Zähne waren bereits in der Planung.")
            st.rerun()

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# Agent-Bereich
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("🤖 KI-Agenten: GOZ-Vorschläge")

if gap:
    btn_label = f"🚀 Agenten starten ({len(gap)} Zahn/Zähne ohne GOZ)"
else:
    btn_label = "🚀 Agenten starten (vollständige GOZ-Analyse)"

col_btn1, col_btn2, _ = st.columns([2, 1, 1])
with col_btn1:
    start_agents = st.button(
        btn_label,
        type="primary",
        disabled=st.session_state.agent_result is not None,
    )
with col_btn2:
    if st.session_state.agent_result:
        if st.button("🔄 Neu analysieren"):
            # Checkbox-Zustände löschen
            for _k in list(st.session_state.keys()):
                if _k.startswith("goz_cb_"):
                    del st.session_state[_k]
            st.session_state.agent_result = None
            st.session_state.agent_log = []
            st.session_state.manual_goz = []
            st.rerun()

if not gap:
    st.caption("ℹ️ Alle geplanten Zähne haben GOZ-Einträge – Agenten prüfen auf Vollständigkeit und Optimierungspotenzial.")

# Status-Log Anzeige (immer sichtbar)
if st.session_state.agent_log:
    with st.expander("Agent-Log", expanded=False):
        for entry in st.session_state.agent_log:
            st.caption(entry)

if start_agents:
    progress_bar = st.progress(0, text="⏳ Starte KI-Agenten...")
    log_container = st.empty()
    log_container.info("🚀 Initialisiere Agenten...")
    agent_log = []

    n_teeth = max(len(agent_teeth), 1)
    # Phasen: 1× Archiv + n_teeth× GOZ + 1× Qualität
    total_steps = 2 + n_teeth

    def status_cb(msg: str):
        agent_log.append(msg)
        st.session_state.agent_log = agent_log.copy()
        log_container.info(msg)
        # Fortschritt: ✅-Meldungen und laufende GOZ-Zähne zählen
        done_archiv  = sum(1 for m in agent_log if "✅" in m and "Archiv" in m)
        done_quality = sum(1 for m in agent_log if "✅" in m and "Qualitäts" in m)
        goz_in_progress = sum(1 for m in agent_log if "GOZ-Spezialist: Zahn" in m)
        done = done_archiv + min(goz_in_progress, n_teeth) + done_quality
        pct = min(done / total_steps, 0.99)
        progress_bar.progress(pct, text=f"⚙️ {msg[:80]}")

    patient_info = {
        "name": (st.session_state.selected_patient or {}).get("name", ""),
        "vorname": (st.session_state.selected_patient or {}).get("vorname", ""),
        "kurztext": kv.get("kurztext", "") if kv else "",
    }

    with st.spinner("KI-Agenten laufen..."):
        try:
            result = run_hkp_pipeline(
                gap_teeth=agent_teeth,
                existing_goz=kv.get("positionen", []) if kv else [],
                patient_info=patient_info,
                status_callback=status_cb,
            )
            st.session_state.agent_result = result
            st.session_state.agent_log = agent_log
            progress_bar.progress(1.0, text="✅ Fertig!")
            st.rerun()
        except Exception as e:
            progress_bar.empty()
            st.error(f"Fehler beim Ausführen der Agenten: {e}")
            st.exception(e)
            st.stop()

# ── Ergebnis anzeigen ──────────────────────────────────────────────────
if st.session_state.agent_result:
    result = st.session_state.agent_result
    goz_struct = result.get("goz_structured", {})

    tab1, tab2, tab3 = st.tabs(["⚕️ GOZ-Vorschläge", "🗄️ Archiv-Analyse", "🔍 Qualitätsprüfung"])

    # ── TAB 1: Farbcodierte GOZ-Vorschläge ────────────────────────────
    with tab1:
        # Legende (immer zeigen)
        st.markdown(
            "🟢 **Pflicht** &nbsp;&nbsp; 🟡 **Empfohlen** &nbsp;&nbsp; ⬜ **Optional** &nbsp;&nbsp; 🟣 **Manuell** &nbsp;&nbsp; 🔁 **Einmalig/Sitzung** (MKO etc. – nur 1× im KVA)",
            unsafe_allow_html=True,
        )
        if goz_struct.get("gesamtbegruendung"):
            st.caption(goz_struct.get("gesamtbegruendung", ""))
        st.divider()

        for zahn_data in goz_struct.get("zaehne", []):
            zahn_nr = zahn_data.get("zahn", "?")
            behandlung = zahn_data.get("behandlung", "")
            positionen_list = zahn_data.get("positionen", [])

            st.markdown(f"#### 🦷 Zahn {zahn_nr} — {behandlung}")

            # Parse-Fehler für diesen einen Zahn anzeigen
            if zahn_data.get("_parse_error"):
                st.warning(f"⚠️ Zahn {zahn_nr}: JSON-Parse-Fehler")
                if zahn_data.get("_raw"):
                    with st.expander("Rohantwort", expanded=False):
                        st.text(zahn_data["_raw"])
            elif not positionen_list:
                st.warning("Keine Positionen vorgeschlagen.")
            else:
                from config import GOZ_SESSION_EINMALIG as _SESSION_GOZ
                farbe_map = {"gruen": "goz-gruen", "gelb": "goz-gelb"}
                icons = {"gruen": "🟢", "gelb": "🟡"}
                for pos_idx, pos in enumerate(positionen_list):
                    farbe = pos.get("farbe") or "none"
                    css_class = farbe_map.get(farbe, "goz-none")
                    icon = icons.get(farbe, "⬜")
                    goz_nr_pos = pos.get("goz_nr", "")
                    betr_str = ""
                    if pos.get("faktor") and pos.get("anzahl"):
                        betr_str = f" | ×{pos['faktor']} × {pos['anzahl']}"
                    beg = pos.get("begruendung", "")
                    # Session-GOZ Badge
                    session_badge = " 🔁" if goz_nr_pos in _SESSION_GOZ else ""

                    # Checkbox: grün/gelb vorausgewählt, optional nicht
                    cb_key = f"goz_cb_{zahn_nr}_{goz_nr_pos}_{pos_idx}"
                    if cb_key not in st.session_state:
                        st.session_state[cb_key] = farbe in ("gruen", "gelb")

                    col_cb, col_pos = st.columns([0.6, 11.4])
                    with col_cb:
                        st.checkbox("", key=cb_key, label_visibility="collapsed")
                    with col_pos:
                        st.markdown(
                            f'<div class="{css_class}">'
                            f'{icon} <b>{goz_nr_pos}</b>{session_badge} – {pos.get("text","")}'
                            f'{betr_str}'
                            f'{"  <i>→ " + beg + "</i>" if beg else ""}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

            # Manuell hinzugefügte Positionen für diesen Zahn
                manual_for_zahn = [m for m in st.session_state.manual_goz if m.get("zahn") == zahn_nr]
                for m_idx, m in enumerate(manual_for_zahn):
                    farbe_m = m.get("farbe", "none")
                    css_m = {"gruen": "goz-gruen", "gelb": "goz-gelb"}.get(farbe_m, "goz-manual")
                    icon_m = {"gruen": "🟢", "gelb": "🟡"}.get(farbe_m, "🟣")
                    cb_key_m = f"goz_cb_manual_{zahn_nr}_{m.get('goz_nr','x')}_{m_idx}"
                    if cb_key_m not in st.session_state:
                        st.session_state[cb_key_m] = True
                    col_cb2, col_pos2 = st.columns([0.6, 11.4])
                    with col_cb2:
                        st.checkbox("", key=cb_key_m, label_visibility="collapsed")
                    with col_pos2:
                        st.markdown(
                            f'<div class="{css_m}">'
                            f'{icon_m} <b>{m["goz_nr"]}</b> – {m["text"]}'
                            f' | ×{m.get("faktor","?")} × {m.get("anzahl",1)}'
                            f'  <i>→ manuell hinzugefügt</i>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                st.markdown("---")

    with tab2:
        st.markdown(result.get("archiv", ""))

    with tab3:
        st.markdown(result.get("qualitaet", ""))

    # ─────────────────────────────────────────────────────────────────────
    # Kostenvoranschlag aus gewählten GOZ-Positionen
    # ─────────────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("💰 Kostenvoranschlag (gewählte Positionen)")

    # Alle angehakten Positionen sammeln
    _selected_rows = []
    for _zd in goz_struct.get("zaehne", []):
        _zahn_nr = _zd.get("zahn", "?")
        for _pi, _pos in enumerate(_zd.get("positionen", [])):
            _cb = f"goz_cb_{_zahn_nr}_{_pos.get('goz_nr','x')}_{_pi}"
            if st.session_state.get(_cb, False):
                _selected_rows.append({
                    "zahn": _zahn_nr,
                    "goz_nr": _pos.get("goz_nr", ""),
                    "text": _pos.get("text", ""),
                    "faktor": float(_pos.get("faktor") or 2.3),
                    "anzahl": int(_pos.get("anzahl") or 1),
                    "farbe": _pos.get("farbe") or "none",
                    "manuell": False,
                })
    # Manuelle Positionen
    for _mi, _m in enumerate(st.session_state.manual_goz):
        _cb_m = f"goz_cb_manual_{_m.get('zahn')}_{_m.get('goz_nr','x')}_{_mi}"
        if st.session_state.get(_cb_m, True):
            _selected_rows.append({
                "zahn": _m.get("zahn", "?"),
                "goz_nr": _m.get("goz_nr", ""),
                "text": _m.get("text", ""),
                "faktor": float(_m.get("faktor") or 2.3),
                "anzahl": int(_m.get("anzahl") or 1),
                "farbe": _m.get("farbe") or "none",
                "manuell": True,
            })

    if not _selected_rows:
        st.info("Noch keine Positionen ausgewählt – Häkchen in den GOZ-Vorschlägen setzen.")
    else:
        # Batch-Preisabfrage
        _all_nrs = list({r["goz_nr"] for r in _selected_rows if r["goz_nr"]})
        _prices = db_module.get_goz_prices_bulk(_all_nrs)

        # Tabelle berechnen
        _table_rows = []
        _total_honorar = 0.0
        _total_mat     = 0.0
        _total_lab     = 0.0

        for _r in _selected_rows:
            _nr  = _r["goz_nr"]
            _p   = _prices.get(_nr, {"base_fee": 0.0, "mat_est": 0.0, "lab_est": 0.0})
            _hon = _p["base_fee"] * _r["faktor"] * _r["anzahl"]
            _mat = _p["mat_est"]  * _r["anzahl"]
            _lab = _p["lab_est"]  * _r["anzahl"]
            _total_honorar += _hon
            _total_mat     += _mat
            _total_lab     += _lab
            _icon = {"gruen": "🟢", "gelb": "🟡"}.get(_r["farbe"], "⬜")
            if _r.get("manuell"):
                _icon = "🟣"
            _table_rows.append({
                "": _icon,
                "Zahn": str(_r["zahn"]),
                "GOZ-Nr.": _nr,
                "Bezeichnung": _r["text"][:45],
                "Faktor": f"×{_r['faktor']:.1f}",
                "Anz.": _r["anzahl"],
                "Honorar €": f"{_hon:>8.2f}",
                "Material €": f"{_mat:>8.2f}" if _mat else "–",
                "Labor €": f"{_lab:>8.2f}" if _lab else "–",
            })

        st.dataframe(_table_rows, use_container_width=True, hide_index=True)

        # Summenspalten
        _total = _total_honorar + _total_mat + _total_lab
        col_k1, col_k2, col_k3, col_k4 = st.columns(4)
        with col_k1:
            st.metric("Honorar (GOZ)", f"{_total_honorar:,.2f} €")
        with col_k2:
            st.metric("Material (Schätzwert)", f"{_total_mat:,.2f} €")
        with col_k3:
            st.metric("Labor (Schätzwert)", f"{_total_lab:,.2f} €")
        with col_k4:
            st.metric("**Gesamt**", f"{_total:,.2f} €")
        st.caption(
            "ℹ️ Honorar = GOZ-Punktzahl × Punktwert (€0,0562421) × Faktor · "
            "Material/Labor = Schätzwerte aus GOZ-Katalog (fremdschaetzbetr + fremdgoldbetr) · "
            "§6-Analog-Positionen: historischer Durchschnitt aus Charly-DB"
        )

    # ─────────────────────────────────────────────────────────────────────
    # GOZ-Selektor (Charly-Style mit Farb-Kodierung)
    # ─────────────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("➕ GOZ-Selektor")

    # Farbsets für Kodierung aufbauen
    existing_nrs = {p.get("goz_nr","") for p in (kv.get("positionen",[]) if kv else []) if p.get("goz_nr")}
    ai_green  = set()
    ai_yellow = set()
    for zd in result.get("goz_structured", {}).get("zaehne", []):
        for pos in zd.get("positionen", []):
            nr = pos.get("goz_nr", "")
            if pos.get("farbe") == "gruen":
                ai_green.add(nr)
            elif pos.get("farbe") == "gelb":
                ai_yellow.add(nr)

    # ── Volltext-Suche ────────────────────────────────────────────────
    col_srch, col_cat = st.columns([2, 2])
    with col_srch:
        goz_search = st.text_input(
            "🔍 Volltext-Suche (GOZ-Nr. oder Beschreibung)",
            placeholder="z.B. Keramik · Implan · 2197 · 8000",
            key="goz_volltext_search",
        )
    with col_cat:
        if not goz_search:
            # Kategorien laden (gecacht in session)
            if "goz_categories" not in st.session_state:
                with st.spinner("Lade Kategorien..."):
                    cats = db_module.get_goz_praxis_categories(limit=40)
                    st.session_state.goz_categories = ["– Kategorie wählen –"] + [c["bezeichnung"] for c in cats]
            sel_cat = st.selectbox("📂 Kategorie", st.session_state.goz_categories, key="goz_cat_sel")
        else:
            sel_cat = None
            st.markdown("&nbsp;")

    # ── GOZ-Items laden ───────────────────────────────────────────────
    goz_items = []
    if goz_search and len(goz_search) >= 2:
        with st.spinner("Suche..."):
            goz_items = db_module.search_goz_volltext(goz_search, limit=60)
    elif sel_cat and sel_cat != "– Kategorie wählen –":
        with st.spinner(f"Lade '{sel_cat}'..."):
            goz_items = db_module.get_goz_by_category(sel_cat, limit=60)

    # ── Farbkodierte Tabelle mit Zeilenauswahl ────────────────────────
    if goz_items:
        import pandas as pd

        # Farben: hoher Kontrast für Dark + Light Mode
        BG_COLORS = {
            "hkp":    ("#4b5563", "#ffffff"),   # Im HKP → dunkelgrau / weiß
            "gruen":  ("#1e7e34", "#ffffff"),   # Pflicht → dunkelgrün / weiß
            "gelb":   ("#c79100", "#ffffff"),   # Empfohlen → dunkles Amber / weiß
            "rot":    ("#bd2130", "#ffffff"),   # Selten → dunkelrot / weiß
        }
        def _status_key(nr):
            if nr in existing_nrs: return "hkp"
            if nr in ai_green:    return "gruen"
            if nr in ai_yellow:   return "gelb"
            return "rot"

        rows = []
        for item in goz_items:
            sk = _status_key(item["nummer"])
            label, _ = _goz_status(item["nummer"], existing_nrs, ai_green, ai_yellow)
            rows.append({
                "GOZ-Nr.":      item["nummer"],
                "Beschreibung": (item.get("bezeichnung") or "")[:70],
                "Ø Faktor":     item.get("avg_faktor", ""),
                "Häufigkeit":   item.get("haeufigkeit", 0),
                "Status":       label,
                "_sk":          sk,
            })
        df = pd.DataFrame(rows)

        # _row_style receives a row from the STYLED df (without _sk, 5 cols)
        # → look up _sk from original df via row.name
        def _row_style(row):
            sk = df.loc[row.name, "_sk"]
            bg, fg = BG_COLORS.get(sk, ("#ffffff", "#000000"))
            return [f"background-color:{bg};color:{fg}"] * len(row)

        styled = df.drop(columns=["_sk"]).style.apply(_row_style, axis=1)

        evt = st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            height=260,
            on_select="rerun",
            selection_mode=["single-row"],
            key="goz_tbl_sel",
        )
        st.caption("✔ Im HKP (grau)  ·  🟢 Pflicht (grün)  ·  🟡 Empfohlen (amber)  ·  🔴 Selten genutzt (rot)  — Zeile klicken → GOZ-Nr. übernehmen")

        # Zeile angeklickt → GOZ-Nr. in Formular übernehmen
        if evt.selection and evt.selection.rows:
            selected_nr = df.iloc[evt.selection.rows[0]]["GOZ-Nr."]
            st.session_state["add_goz_nr_input"] = selected_nr
            # Faktor aus Durchschnitt vorbelegen
            sel_faktor = df.iloc[evt.selection.rows[0]]["Ø Faktor"]
            if sel_faktor:
                try:
                    st.session_state["add_faktor_inp"] = float(sel_faktor)
                except Exception:
                    pass

    # ── Hinzufügen-Formular ───────────────────────────────────────────
    zahn_nummern = [str(t["zahn"]) for t in gap]
    col_a1, col_a2, col_a3, col_a4, col_a5, col_a6 = st.columns([1, 2, 1, 1, 1, 1])
    with col_a1:
        add_zahn = st.selectbox("Zahn", zahn_nummern, key="add_zahn_sel")
    with col_a2:
        add_goz_nr = st.text_input("GOZ-Nr.", placeholder="z.B. 2197 · oder Zeile oben klicken", key="add_goz_nr_input")
    with col_a3:
        add_farbe = st.selectbox("Farbe", ["🟢 Pflicht","🟡 Empfohlen","⬜ Optional"], key="add_farbe_sel")
    with col_a4:
        add_faktor = st.number_input("Faktor", 1.0, 5.0, value=2.3, step=0.1, key="add_faktor_inp")
    with col_a5:
        add_anzahl = st.number_input("Anz.", 1, 20, value=1, key="add_anzahl_inp")
    with col_a6:
        st.markdown("<br>", unsafe_allow_html=True)
        do_add = st.button("➕ Hinzufügen", key="btn_add_goz")

    if do_add:
        nr = add_goz_nr.strip()
        if nr:
            info = db_module.lookup_goz_nr(nr)
            farbe_key = {"🟢 Pflicht":"gruen","🟡 Empfohlen":"gelb","⬜ Optional":None}.get(add_farbe)
            st.session_state.manual_goz.append({
                "zahn": int(add_zahn),
                "goz_nr": nr,
                "text": (info.get("bezeichnung") or info.get("goztext") or nr) if info else nr,
                "faktor": add_faktor,
                "anzahl": add_anzahl,
                "farbe": farbe_key,
                "manuell": True,
            })
            st.success(f"GOZ {nr} → Zahn {add_zahn} hinzugefügt.")
            st.rerun()
        else:
            st.warning("Bitte GOZ-Nr. eingeben oder aus Tabelle kopieren.")

    # ── Manuell hinzugefügte Positionen ──────────────────────────────
    if st.session_state.manual_goz:
        st.caption(f"**{len(st.session_state.manual_goz)} manuell hinzugefügte Position(en):**")
        to_delete = []
        for i, m in enumerate(st.session_state.manual_goz):
            c1, c2 = st.columns([8, 1])
            with c1:
                icon = {"gruen": "🟢", "gelb": "🟡"}.get(m.get("farbe"), "⬜")
                st.caption(f"{icon} Zahn {m['zahn']}: **{m['goz_nr']}** – {m['text'][:60]} | ×{m['faktor']} × {m['anzahl']}")
            with c2:
                if st.button("🗑️", key=f"del_m_{i}"):
                    to_delete.append(i)
        if to_delete:
            st.session_state.manual_goz = [m for j, m in enumerate(st.session_state.manual_goz) if j not in to_delete]
            st.rerun()

    # ─────────────────────────────────────────────────────────────────────
    # Material-Übersicht (aus Charly DB)
    # ─────────────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("📦 Material (aus Charly)")

    mat_data = st.session_state.kv_material or []
    if not mat_data:
        st.info("Keine Materialpositionen im KV vorhanden.")
    else:
        mat_total = sum(float(m.get("betrag") or 0) for m in mat_data)
        st.caption(f"**{len(mat_data)} Positionen | Gesamt: {mat_total:.2f} €**")
        by_phase = defaultdict(list)
        for m in mat_data:
            by_phase[m.get("phase", "–")].append(m)
        for phase, items in by_phase.items():
            with st.expander(f"📋 {phase}", expanded=False):
                for item in items:
                    goz_ref = f" (GOZ {item['goz_nr']})" if item.get('goz_nr') else ""
                    st.markdown(
                        f"- **{item.get('mat_kuerzel','') or item.get('mat_nr','')}** "
                        f"{item.get('mat_bez','')[:60]} "
                        f"| Anz: {item.get('anzahl','?')} "
                        f"| **{float(item.get('betrag') or 0):.2f} €**"
                        f"{goz_ref}"
                    )

    # ─────────────────────────────────────────────────────────────────────
    # Labor-Übersicht (aus Charly DB)
    # ─────────────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("🏭 Labor (aus Charly)")

    labor_data = st.session_state.kv_labor or {}
    lab_summary = labor_data.get("summary", {})
    lab_leistungen = labor_data.get("leistungen", [])

    if not lab_summary and not lab_leistungen:
        st.info("Keine Labordaten im KV vorhanden.")
    else:
        if lab_summary:
            col_l1, col_l2, col_l3 = st.columns(3)
            with col_l1:
                st.metric("Fremdlabor", f"{float(lab_summary.get('fremd_labor') or 0):.2f} €")
            with col_l2:
                st.metric("Labor-Material", f"{float(lab_summary.get('fremd_material') or 0):.2f} €")
            with col_l3:
                total_lab = float(lab_summary.get('fremd_labor') or 0) + float(lab_summary.get('fremd_material') or 0)
                st.metric("Gesamt Labor", f"{total_lab:.2f} €")

        if lab_leistungen:
            with st.expander(f"📋 Labor-Leistungen ({len(lab_leistungen)} Positionen)", expanded=False):
                for lp in lab_leistungen:
                    st.markdown(
                        f"- **{lp.get('nummer','')}** {(lp.get('bezeichnung') or '')[:60]} "
                        f"| Anz: {lp.get('anzahl','?')} | **{float(lp.get('betrag') or 0):.2f} €**"
                    )

    # ─────────────────────────────────────────────────────────────────────
    # Speichern & Export
    # ─────────────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("💾 Speichern & Export")

    col_s1, col_s2, col_s3 = st.columns(3)

    with col_s1:
        approved_by = st.text_input(
            "Freigabe durch (Kürzel/Name)",
            placeholder="Dr. Jung",
            key="approved_by_input",
        )

    with col_s2:
        if st.button("💾 JSON & Analyse-Word speichern", type="primary"):
            with st.spinner("Speichere Projekt..."):
                try:
                    projekt = build_hkp_projekt(
                        kv_details=kv,
                        patient_info=st.session_state.selected_patient or {},
                        gap_teeth=gap,
                        agent_result=result,
                        manual_goz=st.session_state.manual_goz,
                        kv_material=st.session_state.kv_material,
                        kv_labor=st.session_state.kv_labor,
                        approved_by=approved_by or None,
                        selected_positions=_selected_rows if _selected_rows else None,
                        goz_prices=_prices if _prices else None,
                    )
                    ordner = save_projekt(projekt)
                    word_path = ordner / f"HKP_KV{kv['solid']}_Analyse.docx"
                    generate_word(projekt, word_path)
                    st.session_state.saved_ordner = str(ordner)
                    st.session_state.saved_projekt = projekt
                    st.success(f"✅ Gespeichert: {ordner.name}")
                except Exception as e:
                    st.error(f"Fehler beim Speichern: {e}")
                    st.exception(e)

    with col_s3:
        if st.session_state.saved_ordner:
            _gruppiert = st.toggle(
                "Positionen zusammenfassen",
                value=True,
                help="Ein: gleiche GOZ-Nr. über mehrere Zähne zusammengefasst (Standard). "
                     "Aus: jeder Zahn einzeln aufgelistet.",
                key="toggle_gruppiert",
            )
            # Angebot Word erstellen
            if st.button("📄 Angebot erstellen (Word)", type="secondary"):
                with st.spinner("Erstelle Angebot..."):
                    try:
                        from projekt_manager import generate_angebot_word
                        angebot_path = Path(st.session_state.saved_ordner) / f"HKP_KV{kv['solid']}_Angebot.docx"
                        generate_angebot_word(
                            kv_details=kv,
                            patient_info=st.session_state.selected_patient or {},
                            kv_material=st.session_state.kv_material,
                            kv_labor=st.session_state.kv_labor,
                            output_path=angebot_path,
                            selected_positions=_selected_rows if _selected_rows else None,
                            goz_prices=_prices if _prices else None,
                            gruppiert=_gruppiert,
                        )
                        st.session_state.saved_angebot = str(angebot_path)
                        st.success("✅ Angebot erstellt!")
                    except Exception as e:
                        st.error(f"Fehler: {e}")
                        st.exception(e)

    # Download-Buttons
    if st.session_state.saved_ordner:
        dl_col1, dl_col2, dl_col3 = st.columns(3)
        ordner_p = Path(st.session_state.saved_ordner)

        with dl_col1:
            analyse_files = glob.glob(str(ordner_p / "*_Analyse.docx"))
            if analyse_files:
                with open(analyse_files[0], "rb") as f:
                    st.download_button("⬇️ Analyse-Word", data=f.read(),
                        file_name=Path(analyse_files[0]).name,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key="dl_analyse")

        with dl_col2:
            angebot_files = glob.glob(str(ordner_p / "*_Angebot.docx"))
            if angebot_files:
                with open(angebot_files[0], "rb") as f:
                    st.download_button("⬇️ Angebot-Word", data=f.read(),
                        file_name=Path(angebot_files[0]).name,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key="dl_angebot")

        with dl_col3:
            json_path = ordner_p / "projekt_daten.json"
            if json_path.exists():
                with open(json_path, "rb") as f:
                    st.download_button("⬇️ JSON", data=f.read(),
                        file_name=json_path.name, mime="application/json", key="dl_json_main")

        st.caption(f"📁 `{ordner_p.name}`")
