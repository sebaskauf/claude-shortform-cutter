---
name: video-cut-entscheidung
description: Phase B des Longform-Video-Cutters — der redaktionelle Entscheidungs-Layer. Fenster-Agenten (Retakes, False Starts), Global-Pass (Nicht-Content-Sektionen, Supersession), Gegenleser, Merge zu decisions.json. Nutzen nach der Transkription eines Longform-Videos, als zweiter Schritt des video-cutter-Agents, oder bei "entscheide die Cuts".
---

# Phase B: Schnitt-Entscheidungen (WAS fliegt raus)

EISERNE REGEL: Du entscheidest hier nur WAS geschnitten wird — als WORT-IDs.
NIEMALS Sekunden ausgeben. Das WO (exakter Schnittpunkt) macht später
deterministischer Code (Energie-Solver). LLM-Timestamps halluzinieren.

## Der Cut-Typen-Katalog (mit echten Beispielen)

**1. RETAKES (keep-last):** Mehrere Anläufe desselben Satzes, oft 2-8x. Der
LETZTE vollständige gute Take bleibt, alle davor fliegen.
Beispiel: "Perfekt, das— / perfekt, jetzt wo— / perfekt, jetzt wo wirklich
unsere Grundstruktur steht…" → die ersten zwei raus.

**2. FALSE STARTS:** Abgebrochene Satzanfänge, erkennbar am Gedankenstrich oder
abruptem Neuansatz. "ermöglicht Vercel sehr— / ermöglicht Vercel auch zu einem
sehr hohen Grad…" → erster raus.

**3. Stotter-/Phrasen-Wiederholungen MIT Absetzen:** "Wenn du das selber /
Wenn du das selb— / für dich musst— / für dich musst du das am Ende selber
entscheiden." → alles außer dem letzten raus.

**4. META-KOMMENTARE an die Aufnahme:** "warte", "nochmal", "Moment", Flüche
("Oh Scheiße, schon 51, fuck"), "Boom. Boom.", Klatschen-Marker. Immer raus.

**5. NAHE SUPERSESSION:** Er sagt etwas und ersetzt es Sekunden später inhaltlich
("Und das würde ich im zweiten Schritt machen. / Und das ist jetzt das, was ich
im zweiten Schritt machen würde.") → frühere Version raus.

**6. FERNE SUPERSESSION (nur Global-Pass):** Eine Aussage/Sektion wird Minuten
später komplett neu und besser gemacht (z.B. Intro neu aufgenommen). Spätere
Version gewinnt, auch über große Distanz.

**7. NICHT-CONTENT-SEKTIONEN (nur Global-Pass, der größte Hebel!):** Rohe
Aufnahmen enthalten oft ganze Abschnitte, die NICHT ins Video gehören:
- Gesang/Summen in Drehpausen ("She doesn't mind, aight?")
- Mitlaufende Fremd-Audios (TikToks/YouTube auf Englisch)
- Laute DIKTATE an Claude/den Computer ("Okay Claude, ganz andere Sache…",
  "Und da brauche ich mal bitte alle Links… in der PDF") — an die KI gerichtet,
  nicht an den Zuschauer
- Aufnahmen ANDERER Deliverables (z.B. eine Shortform-Reel-Session mitten im
  Tutorial: mehrere Takes eines völlig anderen Skripts)
- Reaktionen/Selbstgespräche beim Warten ("Was laberst du?", "Hä, warum
  funktioniert das?", Kauderwelsch)
- Warm-up-Fragmente am Anfang ("Mm-hmm!", "Ah—", "Jetzt— jetzt— äh—")
Erkennungssignale: Sprachwechsel (englisch mitten im deutschen Tutorial),
direkte KI-Ansprache, Songzeilen-Struktur, kein Bezug zum Tutorial-Thema.

## NICHT schneiden (genauso wichtig)
- Einzelwort-Doppler ohne Pause ("das das") — Schneiden zerhackt das Audio
- Füllwörter in einem sonst guten Satz
- Den finalen Take (nie!)
- Rhetorische Wiederholungen (bewusste Betonung, z.B. Warnung 2x)
- [STILLE]-Zeilen — Dead-Air macht der Solver automatisch
- Ranges, die NUR wegen eines einzelnen seltsamen Wortes auffallen
  (ASR-Halluzination möglich!)

## Ablauf

### B1: Fenster-Agenten (parallel, ein Agent pro win_XX.txt)
Spawne pro Fenster einen general-purpose-Subagenten mit GENAU diesem Prompt
(Datei-Pfad anpassen):

> Du bist Schnitt-Redakteur für ein deutsches Talking-Head-Tutorial
> (verbatim-Transkript mit Versprechern). Lies GENAU EINE Datei:
> <ABSOLUTER_PFAD>/win_XX.txt — Zeilenformat: `wSTART-wEND [zeit] text`.
> FINDE: (1) RETAKES: mehrere Anläufe desselben Satzes → alle außer dem LETZTEN
> vollständigen Take schneiden. (2) FALSE STARTS: abgebrochene Satzanfänge.
> (3) Stotter-Wiederholungen MIT Absetzen. (4) Meta-Kommentare an die Aufnahme.
> (5) NAHE SUPERSESSION: sagt etwas, ersetzt es kurz danach inhaltlich → frühere
> Version schneiden. NICHT schneiden: Einzelwort-Doppler ohne Pause, Füllwörter
> in gutem Satz, den finalen Take, [STILLE]-Zeilen, einmalige Aussagen.
> VORSICHT: best-guess-verbatim — nie einen Cut auf ein einzelnes seltsames
> Wort stützen. Dein Fenster überlappt ±1 Min mit den Nachbarn — schneide auch
> Randbereiche, Dedup übernimmt der Orchestrator.
> Antworte NUR mit JSON: {"cuts":[{"from_id":"w123","to_id":"w145",
> "reason":"…","confidence":0.9,"quote":"erste ~6 Wörter"}]}
> IDs müssen im Fenster existieren, from ≤ to, Ranges = ganze Anlauf-Einheiten.

### B2: Global-Pass (machst DU selbst, nicht delegieren)
Lies script_ids.txt KOMPLETT (bei 60+ Min in 2 Teilen). Identifiziere
Nicht-Content-Sektionen (Typ 6+7) als Ranges mit Grund + Konfidenz und schreibe
sie nach `work/<name>/global_cuts.json`:
`{"sections":[{"from_id":"w0","to_id":"w11","reason":"…","confidence":0.95}]}`
Validiere danach: jede from/to-ID existiert, from ≤ to.
Melde außerdem CONTENT-GAPS (angekündigt, aber nie eingesprochen — z.B.
"jetzt zeige ich euch X" und X kommt nie) für die Review-Liste.

### B3: Merge
Fenster-Ergebnisse als JSON-Array nach `work/<name>/win_results.json` schreiben, dann:
```bash
.venv312/bin/python scripts/merge_decisions.py work/<name>/words_aai.json \
  work/<name>/global_cuts.json work/<name>/win_results.json work/<name>
```
Das Script vereinigt alles, verwirft Einzelwort-Cuts mitten im Fluss und
Konfidenz <0.5, und schreibt decisions.json + decisions_report.md.

### B4: Gegenleser (adversarial, PFLICHT)
Keep-Skript generieren (Transkript minus Cut-Wörter, gleiche fmt-Logik wie
script_ids) und einen FRISCHEN Subagenten darüber schicken: Rest-Dopplungen,
Rest-Müll, kaputte Übergänge (mitten im Gedanken geschnitten), Zuviel-Geschnitten.
Triage seiner Befunde — KONSERVATIV:
- Auto-Anwenden NUR: verwaiste Einzel-Wörter an Cut-Kanten, kaputte
  Zwischenversionen mit vollständiger späterer Version, hängende Ankündigungen
  zu Content-Gaps
- ALLES ANDERE (Intra-Satz-Stotterer, rhetorische Dopplungen) → auf die
  Review-Liste, NICHT blind schneiden (Lektion: zerhacktes Audio)
Nach Auto-Anwendungen: B3 wiederholen.

## Abschluss-Check
- [ ] decisions.json existiert, Cut-Anteil plausibel (Talking-Head-Rohmaterial: 20-50% der Wörter)
- [ ] decisions_report.md gegengelesen: kein Cut ohne Grund
- [ ] Content-Gaps + Gegenleser-Feinschliff für Review-Liste notiert

Weiter mit Skill `video-cut-pipeline`.
