# Setup-Prompt

Diesen Prompt komplett kopieren und in Claude Code pasten — Claude installiert
dann alles mit dir zusammen (die INSTALL.md im Repo ist an Claude adressiert):

---

Ich will mir den Shortform-Cutter einrichten: https://github.com/sebaskauf/claude-shortform-cutter

1. Clone das Repo: `git clone https://github.com/sebaskauf/claude-shortform-cutter.git ~/Documents/Projects/claude-shortform-cutter` (falls der Ordner schon existiert: dort `git pull` statt clone). Geh in den Ordner.
2. Lies die INSTALL.md im Repo und führ sie komplett mit mir durch. Sie ist an dich adressiert: Voraussetzungen prüfen (Python 3.12, ffmpeg, Node), die Python-Umgebung bauen, Agent und Skills mit dem richtigen absoluten Pfad installieren. Was fehlt, installierst du mit mir zusammen — sag mir vorher, was und warum.
3. Für die Transkription brauche ich einen AssemblyAI-Schlüssel: sag mir, wo ich ihn hole (assemblyai.com, kostenloses Konto), und leg ihn als AAI_KEY in die .env (Vorlage .env.example liegt im Repo). Ein Video kostet Cent-Beträge.
4. Installier den broll-ersteller-Agenten mit: https://github.com/sebaskauf/broll-ersteller-agent — ohne ihn läuft der Schnitt, aber es entstehen keine B-Roll-Beats. Prüf danach, dass BEIDE Agenten in ~/.claude/agents/ liegen.
5. Falls ich das Agentic OS nutze: richte zusätzlich den CUTTER-Tab nach INSTALL-AGENTIC-OS.md ein. Falls nicht: alles läuft genauso im Browser.
6. Danach ein Testlauf mit einer kurzen hochkanten Aufnahme (1 bis 2 Minuten): [PFAD ZU EINEM KURZEN TESTVIDEO]. Starte dafür Claude Code einmal neu, damit Agent und Skills geladen sind, und lass dann den shortform-cutter den kompletten Durchlauf machen.
7. Fertig ist es, wenn das Cockpit im Browser aufgeht UND die Timeline Clips zeigt. Prüf das selbst, bevor du es mir meldest — schau auch, ob proxy.mp4 existiert und cockpit.json ein gefülltes src_video hat.

Wenn irgendwas scheitert, zeig mir die genaue Fehlermeldung, statt es als erledigt zu melden.

---

## Danach: ein Reel schneiden

```
Schneide mir dieses Reel: [PFAD ZUM ROHVIDEO]
```

Wenn du mit einem externen Mikrofon aufnimmst, gib die Tonspur gleich mit dazu:

```
Schneide mir dieses Reel: [PFAD ZUM ROHVIDEO]
Die Tonaufnahme vom Mikrofon liegt hier: [PFAD ZUR AUDIODATEI]
```

Der Cutter legt die Mikrofonspur dann millisekundengenau auf das Bild und
verwendet ab da nur noch sie. Ohne zweite Datei nimmt er einfach den
Kameraton — du musst nichts weiter sagen.

## Und dann?

Wenn das Cockpit aufgeht:

1. Schau dir den Schnitt an und korrigier, wo er ein paar Frames daneben liegt.
2. Drück **"B-Roll erstellen"**. Für jeden Clip entsteht ein eigener
   5-Sekunden-Beat, passend zu dem, was du dort sagst. Erster und letzter Clip
   bleiben frei (Hook und Call-to-Action).
3. Warte, bis die Beats fertig sind — sie hängen sich selbst an ihre Clips.
   Der Knopf zeigt den Fortschritt an.
4. Drück **"Neu rendern"**. Fertig ist dein Reel.

Captions macht das System bewusst nicht — die gehen nach dem Export in CapCut
schneller und sehen besser aus.
