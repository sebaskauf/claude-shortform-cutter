---
name: video-cut-pipeline
description: Phase C des Longform-Video-Cutters — deterministische Schnitt-Kette: Energie-Solver, QA-Pyramide über alle Nähte, Auto-Repair, Re-Verify. Plus Infra-Regeln für lange Läufe (Checkpoints, nohup, sequenziell). Nutzen nach den Schnitt-Entscheidungen, als dritter Schritt des video-cutter-Agents.
---

# Phase C: Deterministischer Schnitt + QA (WO geschnitten wird)

Prinzip: Die Entscheidung (Wort-IDs) ist da. Jetzt bestimmt CODE die exakten
Schnittpunkte durch MESSUNG der Waveform — nie durch ASR-Timestamps (die sind
an Versprecher-Stellen 50-400ms daneben, genau dort wo wir schneiden).

Alle Kommandos aus `{{CUTTER_DIR}}`,
Python immer `.venv312/bin/python`.

## ⚠️ Infra-Regeln für lange Läufe (aus schmerzhaften Kills gelernt)
1. **SEQUENZIELL, nie parallel:** Whisper-QA und ffmpeg-Render NIE gleichzeitig.
2. **Läufe >10 Min:** komplett entkoppelt starten:
   `nohup caffeinate -i .venv312/bin/python … > work/<name>/lauf.log 2>&1 & disown`
   Dann per Log-Polling überwachen. Harness-Background-Tasks werden bei langen
   Läufen manchmal gekillt — nohup+disown überlebt das.
3. **QA hat Checkpoint-Resume:** Bei Kill einfach identisch neu starten, sie
   macht ab dem letzten Checkpoint weiter (qa_stage_a.partial.json, alle 10 Nähte).
4. **Memory:** audio_measure.py ist memory-optimiert (float32, Block-RMS).
   Trotzdem: nur EIN whisper-Prozess gleichzeitig.

## C1: Energie-Solver
```bash
.venv312/bin/python scripts/solver_v5.py work/<name>/audio48k.wav \
  work/<name>/words_aai.json work/<name>/decisions.json work/<name>
```
Was er tut: legt jede Schnittkante ins gemessene Stille-Tal (RMS auf den
Samples), nie mitten ins Wort. Kanten ohne echte Pause (>=150ms) werden als
Graufall geflaggt statt geraten. Dead-Air >3s wird komprimiert.
Output: `segments_v5.json`. Plausibilität: Segmente x Keep-Sekunden gegen
Erwartung prüfen (Log-Zeile lesen!).

## C2: QA-Pyramide Stufe A (alle Nähte)
```bash
.venv312/bin/python scripts/qa_stage_a.py work/<name>/audio48k.wav \
  work/<name>/words_aai.json work/<name>/decisions.json \
  work/<name>/segments_v5.json work/<name>
```
Pro Naht: beide Seiten einzeln re-transkribieren (large-v3), Grenzwort-Check
(abgeschnittene Wörter!), Leak-Check (Cut-Material blutet rein), Energie-Check.
Dauer: ~5s/Naht → bei 100+ Nähten als nohup-Lauf (Regel 2).
Output: `qa_stage_a.json` (PASS/FAIL pro Naht).

## C3: Auto-Repair (konservativ)
```bash
.venv312/bin/python scripts/qa_repair.py work/<name>/audio48k.wav \
  work/<name>/words_aai.json work/<name>/decisions.json \
  work/<name>/segments_v5.json work/<name>/qa_stage_a.json \
  work/<name>/segments_v5_repaired.json
```
Repariert nur beweisbare Fälle (Mikro-Scan nach leisen Wortresten an der Kante,
max ±0,8s, nie über Cut-Wörter hinweg). Rest = ehrliche Graufälle.
Log lesen: Repairs plausibel? (z.B. 72ms-Run zurückgeholt = leises Wort ODER
Atmer — QA-Re-Verify entscheidet.)

## C4: Re-Verify
```bash
.venv312/bin/python scripts/qa_stage_a.py work/<name>/audio48k.wav \
  work/<name>/words_aai.json work/<name>/decisions.json \
  work/<name>/segments_v5_repaired.json work/<name>/verify2
```
(Resume greift: unveränderte Nähte werden aus dem Checkpoint übernommen.)

## Fehlerbild → Fix (Tabelle)
| Symptom | Ursache | Fix |
|---|---|---|
| Lauf nach Minuten "killed" | Harness/Sleep/Memory | nohup+caffeinate+disown, QA resumed via Checkpoint |
| boundary_clipped auf Deutsch-Wort | Kante frisst leises Wort | qa_repair macht das; sonst Graufall |
| boundary_clipped auf Englisch-Wort | Prüfer (large-v3 de) germanisiert Englisch | Falscher Alarm → Graufall, kein Auto-Fix |
| cut_text_leaked bei Retakes | Cut-Text ≈ Keep-Text (Retakes sind identisch!) | Nur echter Fail wenn Keep-Text-Check AUCH failt |
| joint_not_silent allein, Text-Checks grün | Atmer/Hall im Nahtfenster | Warnung, kein Fehler |
| erwartetes Wort nicht im Audio hörbar | ASR-Halluzination (best-guess) | Graufall, Kante NICHT erweitern |
| viele in_no_pause/out_no_pause | Cut-Grenzen mitten im Sprachfluss | Entscheidungen prüfen: Ranges auf ganze Anlauf-Einheiten erweitern |

## Abschluss-Check
- [ ] segments_v5_repaired.json existiert
- [ ] Verify2-Bilanz notiert: X PASS / Y Atmer-Warnungen / Z echte Graufälle
- [ ] Graufälle-Anteil <15% der Nähte (sonst zurück zu Phase B)

Weiter mit Skill `video-cut-uebergabe`.

## Windows-Hinweise (Claude Code läuft dort mit Git Bash)

- Python-Pfad: überall `.venv312/Scripts/python` statt `.venv312/bin/python`
- `caffeinate` gibt es nicht — weglassen und stattdessen den Energiesparmodus
  deaktivieren (Einstellungen → Energie), `nohup … & disown` funktioniert in
  Git Bash normal
- Port prüfen/freimachen: statt `lsof -ti :8766` →
  `netstat -ano | findstr :8766`, dann `taskkill //PID <pid> //F`
- ffmpeg installieren: `winget install -e --id Gyan.FFmpeg` · Python 3.12: `winget install Python.Python.3.12`
