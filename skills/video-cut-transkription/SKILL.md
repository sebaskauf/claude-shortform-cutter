---
name: video-cut-transkription
description: Phase A des Longform-Video-Cutters — Intake, Audio-Extrakte, Verbatim-Transkription (AssemblyAI), Halluzinations-Check, ID-Skript + Analyse-Fenster. Nutzen wenn ein rohes Longform-Video für den Schnitt vorbereitet werden soll, als erster Schritt des video-cutter-Agents, oder bei "transkribier das Video für den Schnitt".
---

# Phase A: Video-Intake + Verbatim-Transkription

Ziel dieser Phase: Aus einem rohen Video entsteht `words_aai.json` (jedes Wort mit
ID + Zeiten), `script_ids.txt` (lesbares Skript mit Wort-ID-Ranges) und
`win_XX.txt`-Analysefenster. OHNE verbatim-Transkription sind Versprecher
unsichtbar und können nicht geschnitten werden — deshalb ist diese Phase heilig.

## Feste Pfade
- Projekt: `{{CUTTER_DIR}}`
- Python: IMMER die Projekt-venv (macOS/Linux `.venv312/bin/python`, Windows
  `.venv312/Scripts/python`). Kein System-Python.
- AAI-Key: liegt in `{{CUTTER_DIR}}/.env` als `AAI_KEY`. Laden: `set -a; . ./.env; set +a`
- Workdir pro Video: `work/<kurzname>/` (kurzname = klein, ohne Leerzeichen, z.B. `v7`)

## Schritt 0: Umgebung prüfen (Bootstrap, bei jedem Lauf)
```bash
cd {{CUTTER_DIR}}
.venv312/bin/python -c "import numpy, soundfile, faster_whisper" 2>/dev/null || echo "VENV KAPUTT"
```
Falls VENV KAPUTT (passiert bei Homebrew-Python-Upgrades):
```bash
uv venv --python 3.12 .venv312 2>/dev/null || python3.12 -m venv .venv312
VIRTUAL_ENV=$PWD/.venv312 uv pip install -r requirements.txt 2>/dev/null || .venv312/bin/pip install -r requirements.txt
```

## Schritt 1: Intake
```bash
ffprobe -v error -show_entries format=duration -show_entries stream=codec_type,width,height,r_frame_rate -of csv "<VIDEO>"
```
Notiere: Dauer, Auflösung, fps. Lege `work/<name>/` an. Bei >45 Min: alle
späteren Läufe brauchen die Langvideo-Regeln (siehe Skill video-cut-pipeline).

## Schritt 2: Audio-Extrakte (immer BEIDE)
```bash
ffmpeg -y -loglevel error -i "<VIDEO>" -vn -ac 1 -ar 16000 -c:a pcm_s16le work/<name>/audio16k.wav
ffmpeg -y -loglevel error -i "<VIDEO>" -vn -ac 1 -ar 48000 -c:a pcm_s16le work/<name>/audio48k.wav
```
16k = für ASR. 48k = für den sample-genauen Schnitt-Solver. Nie nur eins.

## Schritt 3: Verbatim-Transkription
```bash
set -a; . ./.env; set +a
.venv312/bin/python scripts/transcribe_aai.py work/<name>/audio16k.wav work/<name>
```
Das Script nutzt AssemblyAI `universal-3-pro` MIT dem best-guess-verbatim-Prompt
(steht im Script). WARUM: Ohne den Prompt unterdrückt das Speech-LLM unsichere
Wörter — genau die Versprecher, die wir schneiden wollen. NIEMALS auf ein
anderes Transkript ausweichen, das Disfluencies glättet (Whisper glättet!).
Dauer: Upload + ~5-10 Min. Bei >30 Min Laufzeit-Erwartung: als nohup-Job (Skill video-cut-pipeline, Infra-Regeln).

## Schritt 4: Halluzinations-Loop-Check (PFLICHT nach jeder Transkription)
```bash
.venv312/bin/python - <<'EOF'
import json, collections
w = json.load(open('work/<name>/words_aai.json'))
texts = [x['text'].lower() for x in w]
grams = collections.Counter(tuple(texts[i:i+5]) for i in range(len(texts)-5))
top = grams.most_common(3)
for g,c in top: print(' '.join(g), '->', c, 'x')
print(len(w), 'Woerter,', w[-1]['end']/60, 'min')
EOF
```
Bewertung: 5-30x dasselbe 5-Gramm = plausibel (echte Retakes!). >100x = 
Halluzinations-Loop → Transkription ist Müll, prüfe Audio + wiederhole.
ZUSATZ-WARNUNG: Der best-guess-Prompt kann einzelne Wörter ERFINDEN (z.B. ein
"Und", das nie gesprochen wurde). Deshalb später nie eine Cut-Entscheidung auf
ein einzelnes seltsames Wort stützen.

## Schritt 5: ID-Skript + Analyse-Fenster generieren
```bash
.venv312/bin/python - <<'EOF'
import json
NAME = '<name>'
w = json.load(open(f'work/{NAME}/words_aai.json'))
lines, cur = [], []
for i, x in enumerate(w):
    cur.append(x)
    gap = (w[i+1]['start'] - x['end']) if i+1 < len(w) else 999
    if x['text'].endswith(('.', '!', '?')) or gap > 0.45:
        lines.append(cur); cur = []
        if gap > 3.0 and i+1 < len(w):
            lines.append([{'id':'', 'text':f'[STILLE {gap:.0f}s — Screen-Demo?]', 'start':x['end'], 'end':w[i+1]['start']}])
if cur: lines.append(cur)
def fmt(ln):
    ids = [x['id'] for x in ln if x['id']]
    txt = " ".join(x['text'] for x in ln)
    if not ids: return f"          [{ln[0]['start']:7.1f}s] {txt}"
    return f"{ids[0]}-{ids[-1]} [{ln[0]['start']:7.1f}s] {txt}"
open(f'work/{NAME}/script_ids.txt','w').write("\n".join(fmt(l) for l in lines))
WIN, OV, t0, n = 600, 60, 0, 0
t_end = w[-1]['end']
while t0 < t_end:
    t1 = min(t0 + WIN + OV, t_end)
    sel = [l for l in lines if l[0]['start'] < t1 and l[-1]['end'] > t0]
    open(f'work/{NAME}/win_{n:02d}.txt','w').write("\n".join(fmt(l) for l in sel))
    n += 1; t0 += WIN
sil = sum(l[0]['end']-l[0]['start'] for l in lines if l[0]['id']=='')
print(f"{len(lines)} Zeilen, {n} Fenster, {sil/60:.1f} min Stille >3s")
EOF
```

## Abschluss-Check der Phase
- [ ] words_aai.json existiert, Wortzahl plausibel (~100-160 Wörter/Sprechminute)
- [ ] Loop-Check bestanden
- [ ] script_ids.txt + win_XX.txt existieren
- [ ] Stille-Anteil notiert (viel Stille = Screen-Demo-Video, siehe Entscheidungs-Skill)

Weiter mit Skill `video-cut-entscheidung`.

## Windows-Hinweise (Claude Code läuft dort mit Git Bash)

- Python-Pfad: überall `.venv312/Scripts/python` statt `.venv312/bin/python`
- `caffeinate` gibt es nicht — weglassen und stattdessen den Energiesparmodus
  deaktivieren (Einstellungen → Energie), `nohup … & disown` funktioniert in
  Git Bash normal
- Port prüfen/freimachen: statt `lsof -ti :8766` →
  `netstat -ano | findstr :8766`, dann `taskkill //PID <pid> //F`
- ffmpeg installieren: `winget install -e --id Gyan.FFmpeg` · Python 3.12: `winget install Python.Python.3.12`
