# DentalAI – HKP-Modul: Claude Code Briefing

## Kontext
Erweiterung der bestehenden DentalAI-Anwendung (Nvidia Spark, Ubuntu,
PostgreSQL, Charly Praxissoftware) um ein HKP-Erstellungsmodul.
On-Premise, keine Cloud, direkt auf Charly-Datenbank.

---

## 1. Charly Datenbankstruktur HKP

### Tabellenhierarchie
```
kv          → HKP-Kopf (1 je Behandlungsfall)
 └── kv_main    → Behandlungsphasen je Zahn/Bereich
      └── kv_daten   → GOZ-Einzelpositionen
```

### Wichtige Spalten

**kv** (Kopf)
```sql
solid       -- PK / HKP-ID
patid       -- FK Patient
kurztext    -- Freitext z.B. "13-23 VK, 37 I3, MKO"
datum       -- ⚠️ Julian-Integer (siehe Abschnitt 2)
honorar     -- Gesamthonorar
material    -- Materialkosten gesamt
labor       -- Laborkosten gesamt
```

**kv_main** (Phasen)
```sql
solid       -- PK
kvid        -- FK → kv.solid
lfdnr       -- Sortierung
zahn        -- FDI-Zahn oder 0 oder Bitmask (noch zu klären)
bezeichnung -- Phase: "PRÄP", "ANPROBE", "EINGLIEDERUNG"
```

**kv_daten** (Positionen)
```sql
solid           -- PK
kvmainid        -- FK → kv_main.solid
lfdnr           -- Sortierung
nummer          -- GOZ-Nr z.B. "2210", "Ä1", "5190a"
bezeichnung     -- Leistungstext (praxisspezifisch!)
gozid           -- FK → goz.solid
mp              -- Steigerungsfaktor (z.B. 2.3, 3.5)
betrag          -- Berechneter Betrag
mwst            -- MwSt
anzahl          -- Anzahl der Leistungen
fuellungszahn   -- Zahnkodierung (siehe Abschnitt 3)
fuellungslage   -- Bitmask für Zahngruppen (siehe Abschnitt 3)
```

**goz** (Stammdaten)
```sql
solid    -- PK
nummer   -- GOZ-Nr (= kv_daten.nummer)
goztext  -- Offizieller Leistungstext
```

**gozdaten** (Praxis-Faktoren)
```sql
gozid    -- FK → goz.solid
mp       -- Standard-Faktor dieser Praxis je Position
```

### Master-JOIN
```sql
SELECT
    kv.solid            AS kv_id,
    kv.kurztext,
    kv.honorar,
    kv.material,
    kv.labor,
    km.solid            AS km_id,
    km.lfdnr            AS phase_nr,
    km.zahn,
    km.bezeichnung      AS phase,
    kd.lfdnr            AS pos_nr,
    kd.nummer           AS goz_nr,
    kd.bezeichnung      AS goz_text,
    kd.mp               AS faktor,
    kd.betrag,
    kd.anzahl,
    kd.fuellungszahn,
    kd.fuellungslage,
    g.goztext           AS goz_langtext,
    gd.mp               AS standard_faktor
FROM public.kv kv
JOIN public.kv_main  km  ON km.kvid     = kv.solid
JOIN public.kv_daten kd  ON kd.kvmainid = km.solid
LEFT JOIN public.goz      g  ON g.nummer  = kd.nummer
LEFT JOIN public.gozdaten gd ON gd.gozid  = g.solid
WHERE kv.patid = <PATID>
ORDER BY km.lfdnr, kd.lfdnr;
```

---

## 2. Datum-Konvertierung (Julian-Integer)

Charly speichert alle Daten als Julian-Integer. Konvertierung:

```sql
-- Anzeige
to_char(('J' || kv.datum)::date + 1, 'DD.MM.YYYY') AS datum_anzeige

-- Jahresfilter
EXTRACT(YEAR FROM ('J' || kv.datum)::date + 1) = 2025

-- Tagesfilter (letzte N Tage)
('J' || kv.datum)::date + 1 >= CURRENT_DATE - INTERVAL '6 days'

-- Als CTE (empfohlen)
WITH kv_dated AS (
    SELECT *, ('J' || datum)::date + 1 AS datum_real
    FROM public.kv
)
```

---

## 3. Zahnkodierung (Bitmask)

### fuellungszahn – Erkennungslogik
```
11 – 48     → direkte FDI-Zahnnummer (Einzelzahn)
0           → kein spezifischer Zahn (allgemeine Leistung)
sonst       → mehrere Zähne → fuellungslage als Bitmask lesen
```

### fuellungslage – Bit → FDI-Mapping
```
Bit  0 = 18    Bit  8 = 21    Bit 16 = 31    Bit 24 = 41
Bit  1 = 17    Bit  9 = 22    Bit 17 = 32    Bit 25 = 42
Bit  2 = 16    Bit 10 = 23    Bit 18 = 33    Bit 26 = 43
Bit  3 = 15    Bit 11 = 24    Bit 19 = 34    Bit 27 = 44
Bit  4 = 14    Bit 12 = 25    Bit 20 = 35    Bit 28 = 45
Bit  5 = 13    Bit 13 = 26    Bit 21 = 36    Bit 29 = 46
Bit  6 = 12    Bit 14 = 27    Bit 22 = 37
Bit  7 = 11    Bit 15 = 28    Bit 23 = 38
```

**Beispiele:**
```
fuellungslage = 2016        → Bits 5-10 → Zähne 13,12,11,21,22,23
fuellungszahn = 1073741824  → Flag (2^30): lies fuellungslage
```

### SQL-Dekodierung (dynamisch, produktionsreif)
```sql
CASE
    WHEN kd.fuellungszahn BETWEEN 11 AND 48
        THEN kd.fuellungszahn::text
    WHEN kd.fuellungszahn = 0
        THEN 'Allgemein'
    ELSE
        (SELECT string_agg(z.fdi::text, ', ' ORDER BY z.fdi)
         FROM (SELECT unnest(ARRAY[
             CASE WHEN (kd.fuellungslage & (1<< 0)) > 0 THEN 18 END,
             CASE WHEN (kd.fuellungslage & (1<< 1)) > 0 THEN 17 END,
             CASE WHEN (kd.fuellungslage & (1<< 2)) > 0 THEN 16 END,
             CASE WHEN (kd.fuellungslage & (1<< 3)) > 0 THEN 15 END,
             CASE WHEN (kd.fuellungslage & (1<< 4)) > 0 THEN 14 END,
             CASE WHEN (kd.fuellungslage & (1<< 5)) > 0 THEN 13 END,
             CASE WHEN (kd.fuellungslage & (1<< 6)) > 0 THEN 12 END,
             CASE WHEN (kd.fuellungslage & (1<< 7)) > 0 THEN 11 END,
             CASE WHEN (kd.fuellungslage & (1<< 8)) > 0 THEN 21 END,
             CASE WHEN (kd.fuellungslage & (1<< 9)) > 0 THEN 22 END,
             CASE WHEN (kd.fuellungslage & (1<<10)) > 0 THEN 23 END,
             CASE WHEN (kd.fuellungslage & (1<<11)) > 0 THEN 24 END,
             CASE WHEN (kd.fuellungslage & (1<<12)) > 0 THEN 25 END,
             CASE WHEN (kd.fuellungslage & (1<<13)) > 0 THEN 26 END,
             CASE WHEN (kd.fuellungslage & (1<<14)) > 0 THEN 27 END,
             CASE WHEN (kd.fuellungslage & (1<<15)) > 0 THEN 28 END,
             CASE WHEN (kd.fuellungslage & (1<<16)) > 0 THEN 31 END,
             CASE WHEN (kd.fuellungslage & (1<<17)) > 0 THEN 32 END,
             CASE WHEN (kd.fuellungslage & (1<<18)) > 0 THEN 33 END,
             CASE WHEN (kd.fuellungslage & (1<<19)) > 0 THEN 34 END,
             CASE WHEN (kd.fuellungslage & (1<<20)) > 0 THEN 35 END,
             CASE WHEN (kd.fuellungslage & (1<<21)) > 0 THEN 36 END,
             CASE WHEN (kd.fuellungslage & (1<<22)) > 0 THEN 37 END,
             CASE WHEN (kd.fuellungslage & (1<<23)) > 0 THEN 38 END,
             CASE WHEN (kd.fuellungslage & (1<<24)) > 0 THEN 41 END,
             CASE WHEN (kd.fuellungslage & (1<<25)) > 0 THEN 42 END,
             CASE WHEN (kd.fuellungslage & (1<<26)) > 0 THEN 43 END,
             CASE WHEN (kd.fuellungslage & (1<<27)) > 0 THEN 44 END,
             CASE WHEN (kd.fuellungslage & (1<<28)) > 0 THEN 45 END,
             CASE WHEN (kd.fuellungslage & (1<<29)) > 0 THEN 46 END
         ]) AS fdi) z WHERE z.fdi IS NOT NULL)
END AS zaehne_decoded
```

---

## 4. GOZ Grundregeln

```
Betrag = Punktzahl × 0,0562421 € × Faktor

Faktor 1,0 – 3,5    → Regelbereich
Faktor > 2,3        → schriftliche Begründung Pflicht
Faktor > 3,5        → nur per §2-Vereinbarung
Regelfall           → 2,3-fach
```

### GOZ-Abschnitte (für HKP relevant)
```
A  0010–0120   Allgemein, HKP-Erstellung (0030, 0040)
B  1000–1040   Prophylaxe
C  2000–2440   Konservierend, Füllungen, Kronen (2210, 2197, 2170...)
D  3000–3310   Chirurgie
E  4000–4150   Parodontologie (4050, 4055, 4060)
F  5000–5340   Prothetik, Brücken (5110, 5170, 5190...)
G  6000–6260   KFO
H  7000–7100   Aufbissbehelf
J  8000–8100   Funktionsanalytik / MKO-Paket
K  9000–9170   Implantologie
Ä  GOÄ-Nrn.   Ärztliche Leistungen (Ä1, Ä5, Ä5000...)
```

### §6-Analog-Positionen ⚠️
```
Leistungen nach 1988 entwickelt → nicht im GOZ-Standard
→ Abrechnung analog zu vorhandener GOZ-Position
→ In Charly als praxisspezifischer Alias mit Suffix:
   "2120z"   dentinadhäsive Aufbaufüllung
   "5190a"   Abformung individueller Löffel
   "5110a"   Dentinversiegelung
   "2170zwei" Inlay mehr als zweiflächig
   "0090ok"  Infiltrationsanästhesie OK (Kieferhälfte)

→ Stehen NICHT im GOZ-Katalog
→ Müssen aus kv_daten der Praxis extrahiert werden:
```
```sql
SELECT DISTINCT nummer, bezeichnung, COUNT(*) AS haeufigkeit
FROM public.kv_daten
WHERE nummer NOT IN (SELECT nummer FROM public.goz)
  AND nummer NOT LIKE 'Ä%'
  AND nummer != '_'
GROUP BY nummer, bezeichnung
ORDER BY haeufigkeit DESC;
```

### Kostenstruktur HKP
```
Gesamtkosten
├── Honorar    → GOZ-Positionen × Faktor      (kv_daten)
├── Labor      → BEB-Positionen, frei kalkuliert (separate Tabelle?)
└── Material   → Nur was GOZ explizit erlaubt  (separate Tabelle?)
```

---

## 5. Praxisspezifische Erkenntnisse (VK-Behandlungen 2025, n=40)

### Klassifikationsschema
```
≥ 80%  → pflicht   (automatisch in HKP setzen)
50-79% → standard  (vorgeschlagen, abwählbar)
10-49% → optional  (Checkbox)
< 10%  → selten    (nur auf Anfrage)
```

### Erkannte GOZ-Positionen nach Häufigkeit
```
95%  2030   Besondere Maßnahmen Präparieren/Füllen  → PFLICHT
85%  8000   Klinische Funktionsanalyse (MKO)        → PFLICHT*
85%  8010   Registrieren Zentrallage (MKO)          → PFLICHT*
85%  8020   Scharnierachsenbestimmung (MKO)         → PFLICHT*
85%  8060   Artikulator-Einstellung (MKO)           → PFLICHT*
85%  8080   Diagnostik Modelle (MKO)                → PFLICHT*
72%  5190a  Abformung individ. Löffel §6            → STANDARD
72%  0040   HKP-Erstellung KFO/Funktionsanalytik   → STANDARD
70%  4050   Zahnreinigung einwurzelig               → STANDARD
67%  2270   Provisorium je Zahn                    → STANDARD
55%  2120z  Aufbaufüllung §6                       → STANDARD
52%  Ä1    Beratung (GOÄ)                          → STANDARD
52%  5110a  Dentinversiegelung §6                  → STANDARD
42%  0090   Infiltrationsanästhesie                → OPTIONAL
35%  0070   Vitalitätsprüfung                      → OPTIONAL
25%  0100   Leitungsanästhesie                     → OPTIONAL
25%  2290   Entfernung Krone/Inlay                 → OPTIONAL
 7%  5120   Provisorische Brücke                   → SELTEN

* MKO = Myozentrische Okklusion – Philosophiepaket dieser Praxis
  (nicht aus GOZ ableitbar, nur aus historischen HKPs erkennbar)
```

### LLM vs. Realität – bekannte Abweichungen
```
LLM übersieht:  §6-Analog-Positionen (kennt nur Standard-GOZ)
LLM übersieht:  praxisspezifische Aliase (0090ok, 2170zwei...)
LLM falsch:     Ausschluss-Regel zu weit (gilt nur am SELBEN Zahn)
LLM falsch:     MKO-Paket als "optional" (ist hier PFLICHT)
→ Deshalb immer: LLM-Output → SQL-Retrieval auf historischen HKPs
```

---

## 6. Testdaten finden (letzte 6 Tage = meine Einträge)

### Alle neuen HKPs der letzten 6 Tage
```sql
SELECT
    kv.solid        AS kv_id,
    kv.patid,
    kv.kurztext,
    to_char(('J' || kv.datum)::date + 1, 'DD.MM.YYYY') AS datum,
    kv.honorar,
    kv.material,
    kv.labor
FROM public.kv
WHERE ('J' || kv.datum)::date + 1 >= CURRENT_DATE - INTERVAL '6 days'
ORDER BY kv.datum DESC, kv.solid DESC;
```

### Mit allen Positionen aufgeklappt
```sql
SELECT
    kv.solid        AS kv_id,
    kv.kurztext,
    to_char(('J' || kv.datum)::date + 1, 'DD.MM.YYYY') AS datum,
    km.bezeichnung  AS phase,
    kd.lfdnr,
    kd.nummer       AS goz_nr,
    kd.bezeichnung,
    kd.mp           AS faktor,
    kd.betrag,
    kd.anzahl,
    kd.fuellungszahn,
    kd.fuellungslage
FROM public.kv kv
JOIN public.kv_main  km ON km.kvid     = kv.solid
JOIN public.kv_daten kd ON kd.kvmainid = km.solid
WHERE ('J' || kv.datum)::date + 1 >= CURRENT_DATE - INTERVAL '6 days'
ORDER BY kv.solid DESC, km.lfdnr, kd.lfdnr;
```

### Nur neue Patienten (falls Testpatienten angelegt)
```sql
SELECT solid, name, vorname,
    to_char(('J' || gebdatum)::date + 1, 'DD.MM.YYYY') AS geburtsdatum
FROM public.pat
WHERE ('J' || anlagedatum)::date + 1 >= CURRENT_DATE - INTERVAL '6 days'
ORDER BY solid DESC;
-- ⚠️ Spaltenname anlagedatum ggf. anpassen
```

### Alle neuen Datenbankeinträge (tabellenübergreifend, Orientierung)
```sql
-- Zeigt in welchen Tabellen überhaupt neue Einträge existieren
-- Nur für Tabellen mit einem 'datum'-ähnlichen Feld nutzbar
-- Besser: direkt die kv-Tabelle als Anker nutzen (siehe oben)

SELECT 'kv'       AS tabelle, COUNT(*) AS neue_eintraege
FROM public.kv
WHERE ('J' || datum)::date + 1 >= CURRENT_DATE - INTERVAL '6 days'
UNION ALL
SELECT 'kv_main', COUNT(*)
FROM public.kv_main km
JOIN public.kv kv ON kv.solid = km.kvid
WHERE ('J' || kv.datum)::date + 1 >= CURRENT_DATE - INTERVAL '6 days'
UNION ALL
SELECT 'kv_daten', COUNT(*)
FROM public.kv_daten kd
JOIN public.kv_main km ON km.solid = kd.kvmainid
JOIN public.kv kv      ON kv.solid = km.kvid
WHERE ('J' || kv.datum)::date + 1 >= CURRENT_DATE - INTERVAL '6 days';
```

---

## 7. Erste Aufgaben für Claude Code

```
[ ] Testdaten lokalisieren (Abschnitt 6 – letzte 6 Tage)
[ ] Tabellenschema vollständig kartieren:
      SELECT table_name FROM information_schema.tables
      WHERE table_schema = 'public' ORDER BY table_name;
[ ] Materialtabelle finden (kv.material ist nur Summe)
[ ] Labortabelle finden (kv.labor ist nur Summe)
[ ] §6-Analog-Positionen dieser Praxis extrahieren (Abschnitt 4)
[ ] kv_main.zahn = 133088 klären (Bitmask? FDI? Konstante?)
[ ] Julian-Konvertierung verifizieren:
      bekanntes Datum nehmen → hin- und zurückrechnen → prüfen
[ ] Statistik-Query ausführen (Abschnitt 5 Klassifikation)
[ ] Master-JOIN mit einem bekannten HKP testen
```
