# Installation — an Claude adressiert

Diese Datei ist an dich, Claude, gerichtet. Führ sie mit dem Nutzer zusammen
durch. Sag vor jedem Schritt, was du tust und warum. Wenn etwas scheitert, zeig
die genaue Fehlermeldung, statt weiterzumachen.

## 1. Voraussetzungen prüfen

```bash
python3 --version          # 3.12 oder neuer
ffmpeg -version | head -1  # muss da sein
node --version             # für die B-Roll-Beats (HyperFrames)
```

Fehlt etwas, installier es zusammen mit dem Nutzer:
- macOS: `brew install python@3.12 ffmpeg node`
- Debian/Ubuntu: `sudo apt install python3.12 python3.12-venv ffmpeg nodejs`

## 2. Python-Umgebung

Im Repo-Ordner:

```bash
python3.12 -m venv .venv312
.venv312/bin/pip install --upgrade pip
.venv312/bin/pip install -r requirements.txt
```

Prüf danach, dass die Kernmodule laden:

```bash
.venv312/bin/python -c "import numpy, soundfile, PIL; print('ok')"
```

## 3. AssemblyAI-Schlüssel

Der Nutzer holt sich einen kostenlosen Schlüssel auf assemblyai.com. Leg ihn
als `AAI_KEY` in eine `.env` im Repo-Ordner (Vorlage: `.env.example`). Ein
15-Minuten-Video kostet dort Cent-Beträge.

## 4. Agent installieren

Kopier den Agenten nach `~/.claude/agents/` und trag den echten Repo-Pfad ein:

```bash
mkdir -p ~/.claude/agents
cp agent/shortform-cutter.md ~/.claude/agents/
```

Öffne die Datei danach und ersetz jede Erwähnung von `scripts/` durch den
absoluten Pfad dieses Repos, damit der Agent die Werkzeuge findet.

## 5. Skills installieren

```bash
mkdir -p ~/.claude/skills
cp -r skills/* ~/.claude/skills/
```

## 6. Den broll-ersteller mitinstallieren

Ohne ihn läuft Phase A vollständig, aber es entstehen keine B-Roll-Beats.

```bash
git clone https://github.com/sebaskauf/broll-ersteller-agent.git ~/Documents/Projects/broll-ersteller-agent
cd ~/Documents/Projects/broll-ersteller-agent && sh install.sh
```

Prüf danach, dass beide Agenten da sind:

```bash
ls ~/.claude/agents/ | grep -E "shortform-cutter|broll-ersteller"
```

Beide Namen müssen erscheinen. Der Cutter beauftragt den broll-ersteller
später direkt über `claude -p --agent broll-ersteller` — **setz dabei niemals
`--permission-mode`**, der Agent bringt seinen eigenen mit. Ein überschriebener
Modus nimmt ihm die Bash-Rechte, und er wartet dann unbeaufsichtigt ewig auf
eine Freigabe, die niemand geben kann.

## 7. Claude Code neu starten

Agent und Skills werden beim Start geladen. Ohne Neustart findet Claude den
Agenten nicht.

## 8. Testlauf

Nimm eine kurze hochkante Aufnahme (ein bis zwei Minuten) und lass den
`shortform-cutter` den kompletten Durchlauf machen. Fertig ist es, wenn:

- das Cockpit im Browser aufgeht und die Timeline Clips zeigt
- `proxy.mp4` im Workdir existiert
- `cockpit.json` ein gefülltes `src_video` hat

Prüf diese drei Punkte selbst, bevor du "fertig" sagst. Genau sie fehlten in
einem echten Lauf und machten das Ergebnis unbenutzbar, obwohl der Agent
"Cockpit läuft" gemeldet hatte.

## Bekannte Stolpersteine

**Das Cockpit zeigt keine Clips.** Schau in die Statuszeile unten. Ein
Typfehler in `cockpit_overrides.json` (etwa `"broll": {}` statt `[]`) bricht
das Zeichnen der Timeline ab, bevor der erste Clip entsteht — sichtbar nur
dort, nicht in der Browser-Konsole.

**Die Overlay-Vorschau bleibt aus.** Dann wurde der Server ohne Quellvideo
gestartet. Es gehört als fünftes Argument dazu.

**Änderungen am Cockpit wirken nicht.** Ein laufender Server hat den alten Code
im Speicher. Beende ihn wirklich und prüf, dass kein zweiter weiterläuft.
