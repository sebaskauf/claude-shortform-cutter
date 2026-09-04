# Installation — an Claude adressiert

Diese Datei ist an dich, Claude, gerichtet. Der Nutzer ist in der Regel kein
Entwickler. Führ ihn durch, erklär in normalem Deutsch, mach so viel wie möglich
selbst, und melde nichts als erledigt, was du nicht geprüft hast.

## Schritt 0 — Inventur, bevor du irgendetwas installierst

Prüf zuerst alles und zeig dem Nutzer eine Übersicht. Erst danach installierst
du.

```bash
python3 --version
python3.12 --version 2>/dev/null || echo "python3.12 nicht als eigener Befehl"
ffmpeg -version 2>/dev/null | head -1 || echo "ffmpeg FEHLT"
node --version 2>/dev/null || echo "node FEHLT"
git --version
```

Sag ihm zu jedem Punkt einen Satz, wofür es gebraucht wird:

| Baustein | wofür |
|---|---|
| Python 3.12+ | die Schnitt-Werkzeuge |
| ffmpeg | Video schneiden und rendern |
| Node | die B-Roll-Beats (HyperFrames) |
| git | das Repo holen und aktuell halten |

Was fehlt, installierst du zusammen mit ihm:

- **macOS:** `brew install python@3.12 ffmpeg node`
  (kein Homebrew? Dann zuerst: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`)
- **Debian/Ubuntu:** `sudo apt update && sudo apt install python3.12 python3.12-venv ffmpeg nodejs git`

Sag vorher, was du installierst und wie lange es ungefähr dauert. Homebrew beim
ersten Mal kann zehn Minuten brauchen — das soll ihn nicht erschrecken.

## Schritt 1 — Python-Umgebung

Im Repo-Ordner:

```bash
python3.12 -m venv .venv312
.venv312/bin/pip install --upgrade pip
.venv312/bin/pip install -r requirements.txt
```

Danach **prüfen, nicht annehmen**:

```bash
.venv312/bin/python -c "import numpy, soundfile, PIL, rapidfuzz; print('ok')"
```

Kommt kein `ok`, ist die Installation fehlgeschlagen. Zeig die Ausgabe und
behebe es, bevor du weitermachst.

## Schritt 2 — AssemblyAI-Schlüssel

Das ist der einzige Schritt, den nur der Nutzer machen kann. Führ ihn wirklich
durch, statt nur einen Link zu nennen.

**Sag ihm zuerst das hier:**

> Für die Transkription braucht das System einen Zugang zu AssemblyAI — das ist
> der Dienst, der aus deinem Video den Text macht, inklusive der Versprecher.
> Ohne die kann das System nicht erkennen, wo du dich verhaspelt und neu
> angesetzt hast. Das Konto ist kostenlos und bringt ein Startguthaben mit, mit
> dem du erstmal ohne Kreditkarte loslegen kannst. Ein Video von zehn Minuten
> kostet davon nur Cent-Beträge.

**Dann führ ihn Klick für Klick:**

1. Geh auf **assemblyai.com** und klick oben rechts auf **Sign Up**.
2. Registrier dich mit E-Mail oder Google-Konto.
3. Nach der Anmeldung landest du im Dashboard. Dort steht gleich oben
   **"Your API key"** mit einer langen Zeichenfolge daneben.
4. Klick auf das Kopier-Symbol daneben.
5. Sag mir Bescheid, wenn du ihn hast.

**Warte hier wirklich, bis er antwortet.** Mach nicht weiter, während er noch
sucht.

Wenn er den Schlüssel hat, trag ihn selbst ein:

```bash
cp .env.example .env
```

Dann in der `.env` die Zeile `AAI_KEY=` um seinen Schlüssel ergänzen — der
Feldname muss exakt `AAI_KEY` heißen, so sucht ihn der Code.

**Und prüf ihn mit einem echten Aufruf**, nicht nur ob die Datei existiert:

```bash
curl -s -H "authorization: <SCHLÜSSEL>" https://api.assemblyai.com/v2/transcript?limit=1 | head -c 200
```

Kommt eine JSON-Antwort zurück, stimmt der Schlüssel. Kommt `{"error":...}`
oder `Unauthorized`, sag konkret was los ist:

- **Unauthorized / invalid** → Der Schlüssel ist falsch kopiert. Häufig ist ein
  Leerzeichen am Anfang oder Ende mit drin. Lass ihn nochmal kopieren.
- **Nichts kommt zurück** → keine Internetverbindung oder Tippfehler in der URL.
- **Konto nicht bestätigt** → Er muss erst die Bestätigungsmail anklicken.

## Schritt 3 — Agent installieren

```bash
mkdir -p ~/.claude/agents
cp agent/shortform-cutter.md ~/.claude/agents/
```

Öffne die kopierte Datei danach und ersetz jede Erwähnung von `scripts/` durch
den **absoluten Pfad** dieses Repos, damit der Agent seine Werkzeuge findet.
Beispiel: `scripts/audio_sync.py` wird zu
`/Users/<name>/Documents/Projects/claude-shortform-cutter/scripts/audio_sync.py`.

## Schritt 4 — Skills installieren

```bash
mkdir -p ~/.claude/skills
cp -r skills/* ~/.claude/skills/
```

## Schritt 5 — Den broll-ersteller mitinstallieren

Ohne ihn läuft der Schnitt vollständig, aber es entstehen keine B-Roll-Beats.

```bash
git clone https://github.com/sebaskauf/broll-ersteller-agent.git ~/Documents/Projects/broll-ersteller-agent
cd ~/Documents/Projects/broll-ersteller-agent && sh install.sh
```

Prüf danach selbst und zeig dem Nutzer das Ergebnis:

```bash
ls ~/.claude/agents/ | grep -E "shortform-cutter|broll-ersteller"
```

Beide Namen müssen erscheinen.

⚠️ **Wichtig für später:** Der Cutter beauftragt den broll-ersteller über
`claude -p --agent broll-ersteller`. Setz dabei **niemals** `--permission-mode`.
Der Agent bringt seinen eigenen mit. Ein überschriebener Modus nimmt ihm die
Rechte, Programme auszuführen — er wartet dann unbeaufsichtigt ewig auf eine
Freigabe, die niemand geben kann, und sieht dabei aus, als würde er arbeiten.

## Schritt 6 — Claude Code neu starten

Agent und Skills werden beim Start geladen. Sag dem Nutzer ausdrücklich, dass er
Claude Code jetzt einmal beenden und neu öffnen muss. Ohne Neustart findet
Claude den Agenten nicht, und ihr sucht beide an der falschen Stelle.

## Schritt 7 — Testlauf

Frag nach einer kurzen hochkanten Aufnahme (ein bis zwei Minuten). Hat er keine,
sag ihm, er soll einfach kurz etwas mit dem Handy aufnehmen — Inhalt egal.

Lass dann den `shortform-cutter` den kompletten Durchlauf machen.

**Fertig ist es erst, wenn du diese drei Dinge selbst geprüft hast:**

1. Das Cockpit geht im Browser auf **und die Timeline zeigt Clips**
2. `proxy.mp4` existiert im Arbeitsordner
3. `cockpit.json` hat ein gefülltes `src_video`

Genau diese drei fehlten in einem echten Lauf und machten das Ergebnis
unbenutzbar, obwohl der Agent "Cockpit läuft" gemeldet hatte. Prüf sie, bevor du
etwas meldest.

## Zum Schluss

Sag dem Nutzer in drei Sätzen, was er jetzt tun kann:

> Dein Setup steht. Um ein Video zu schneiden, sagst du mir einfach: "Schneide
> mir dieses Reel: <Pfad zur Datei>". Wenn du mit einem externen Mikrofon
> aufnimmst, gib die Tonspur gleich mit dazu.

## Bekannte Stolpersteine

**Das Cockpit zeigt keine Clips.** Schau in die Statuszeile unten im Cockpit.
Ein Typfehler in `cockpit_overrides.json` (etwa `"broll": {}` statt `[]`) bricht
das Zeichnen der Timeline ab, bevor der erste Clip entsteht — sichtbar nur dort,
nicht in der Browser-Konsole.

**Die Overlay-Vorschau bleibt aus.** Dann wurde der Server ohne Quellvideo
gestartet. Es gehört als fünftes Argument dazu.

**Änderungen am Cockpit wirken nicht.** Ein laufender Server hat den alten Code
im Speicher. Beende ihn wirklich und prüf, dass kein zweiter weiterläuft:
`pgrep -f cockpit_server`.

**Der Schnitt klingt an vielen Nähten hart.** Erster Verdacht ist die
Sprech-Schwelle bei externem Mikrofon — siehe Gesetz 13 im Agent-File.
