# Setup-Prompt

Diesen Prompt komplett kopieren und in Claude Code pasten. Claude prüft dann
zuerst, was auf deinem Rechner schon da ist und was fehlt, und richtet dir alles
Fehlende Schritt für Schritt ein — du musst nichts vorher installiert haben.

---

Ich will mir den Shortform-Cutter einrichten: https://github.com/sebaskauf/claude-shortform-cutter

Richte mir das komplett ein. Ich bin kein Entwickler — nimm mich an die Hand,
erklär mir jeden Schritt in normalem Deutsch und mach so viel wie möglich
selbst. Frag mich nur, wenn du etwas brauchst, das nur ich holen kann.

**So gehst du vor:**

1. **Erst Inventur, dann installieren.** Prüf zuerst, was auf meinem Rechner
   schon vorhanden ist: Python 3.12 oder neuer, ffmpeg, Node, git, Claude Code.
   Zeig mir eine kurze Übersicht, was da ist und was fehlt — mit einem Satz je
   Punkt, wofür das gebraucht wird. Erst danach fängst du an zu installieren.

2. **Repo holen.** `git clone https://github.com/sebaskauf/claude-shortform-cutter.git ~/Documents/Projects/claude-shortform-cutter`
   (falls der Ordner schon existiert: dort `git pull` statt clone).

3. **INSTALL.md lesen und komplett mit mir durchführen.** Sie ist an dich
   adressiert und enthält alle Details. Was fehlt, installierst du mit mir
   zusammen — sag mir vorher, was du installierst und warum.

4. **AssemblyAI-Schlüssel.** Den brauche ich für die Transkription, und den
   kann nur ich holen. Führ mich Schritt für Schritt durch:
   - erklär mir in zwei Sätzen, wofür der Schlüssel da ist und was er kostet
   - sag mir genau, was ich klicken muss (Seite, Knopf, wo der Schlüssel steht)
   - warte, bis ich ihn habe, und frag mich dann danach
   - trag ihn selbst in die `.env` ein und prüf mit einem echten Testaufruf, ob
     er funktioniert — nicht nur, ob die Datei existiert
   Wenn der Schlüssel nicht funktioniert, sag mir konkret was falsch ist
   (Tippfehler, falsche Zeile, Konto nicht bestätigt) statt nur "Fehler".

5. **Beide Agenten installieren.** Der Cutter allein schneidet nur — für die
   B-Roll-Beats brauche ich zusätzlich den broll-ersteller:
   https://github.com/sebaskauf/broll-ersteller-agent
   Prüf danach selbst, dass BEIDE in `~/.claude/agents/` liegen, und zeig mir
   das Ergebnis.

6. **Agentic OS.** Frag mich, ob ich es nutze. Falls ja: richte den CUTTER-Tab
   nach INSTALL-AGENTIC-OS.md ein. Falls nein: überspring das, alles läuft
   genauso im Browser.

7. **Claude Code neu starten lassen.** Sag mir, dass ich das jetzt machen muss,
   damit Agent und Skills geladen werden. Ohne Neustart findet Claude den
   Agenten nicht.

8. **Testlauf.** Frag mich nach einer kurzen hochkanten Aufnahme (ein bis zwei
   Minuten reichen). Wenn ich keine habe, sag mir, dass ich einfach kurz was mit
   dem Handy aufnehmen soll. Dann lass den `shortform-cutter` den kompletten
   Durchlauf machen.

9. **Selbst prüfen, bevor du fertig meldest.** Ruf das Cockpit auf und
   kontrollier drei Dinge: Die Timeline zeigt Clips, `proxy.mp4` existiert im
   Arbeitsordner, und `cockpit.json` hat ein gefülltes `src_video`. Erst wenn
   alle drei stimmen, sagst du mir, dass es fertig ist.

**Grundregeln für dich:**

- Wenn etwas scheitert, zeig mir die genaue Fehlermeldung und was du dagegen
  tun willst — nicht "es gab ein Problem".
- Erklär mir Fachbegriffe beim ersten Mal in einem Halbsatz.
- Melde nichts als erledigt, was du nicht selbst geprüft hast.
- Am Ende: sag mir in drei Sätzen, was ich jetzt tun kann und wie ich mein
  erstes Video schneide.

---

## Danach: ein Reel schneiden

```
Schneide mir dieses Reel: [PFAD ZUM ROHVIDEO]
```

Wenn du mit einem externen Mikrofon aufnimmst, gib die Tonspur gleich dazu:

```
Schneide mir dieses Reel: [PFAD ZUM ROHVIDEO]
Die Tonaufnahme vom Mikrofon liegt hier: [PFAD ZUR AUDIODATEI]
```

Der Cutter legt die Mikrofonspur dann millisekundengenau auf das Bild und
verwendet ab da nur noch sie. Ohne zweite Datei nimmt er den Kameraton — du
musst nichts weiter sagen.

## Und dann?

Wenn das Cockpit im Browser aufgeht:

1. Schau dir den Schnitt an und korrigier, wo er ein paar Frames daneben liegt.
2. Drück **"B-Roll erstellen"**. Für jeden Clip entsteht ein eigener
   5-Sekunden-Beat, passend zu dem, was du dort sagst. Erster und letzter Clip
   bleiben frei (Hook und Call-to-Action).
3. Warte, bis die Beats fertig sind — sie hängen sich selbst an ihre Clips, der
   Knopf zeigt den Fortschritt.
4. Drück **"Neu rendern"**. Fertig ist dein Reel.

Captions macht das System bewusst nicht — die gehen nach dem Export in CapCut
schneller und sehen besser aus.
