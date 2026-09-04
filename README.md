# Claude Shortform-Cutter

Ein Claude-Code-System, das hochkante Talking-Head-Aufnahmen zu fertigen Reels
schneidet: Verbatim-Transkription, Take-Auswahl, sample-genauer Schnitt an
echten Pausen, Farb-Look — und dann **ein B-Roll pro Clip auf Knopfdruck**,
automatisch an die Clip-Länge angepasst.

Das ist das echte Produktions-Setup von [Sebastian Kauffmann](https://www.skool.com/skaile)
(SKAILE Academy) für seine täglichen Reels.

## Was das System macht

**Phase A — du gibst das Rohvideo rein.**

1. **Mikrofon-Sync**, falls du mit einem externen Mikro aufnimmst: Die
   Tonspur wird auf die Millisekunde genau auf das Bild gelegt, danach zählt
   nur noch das Mikrofon. Ohne zweite Aufnahme passiert nichts — dann ist die
   Kameraspur die Tonspur.
2. **Verbatim-Transkription** (AssemblyAI) plus **Gap-Probe**: Alle Stillen ab
   einer Sekunde werden auf Energie geprüft und aktive Lücken mit Whisper
   nachtranskribiert. Ohne diesen Schritt verschluckt die Transkription
   nachweislich ganze Takes — in einem gemessenen Fall 70 Sekunden Sprache.
3. **Take-Auswahl** nach keep-last, danach ein **Energie-Solver**, der die
   Schnittpunkte aus der Waveform misst statt sie zu schätzen.
4. **Farb-Look** automatisch auf jeden Talking-Head-Clip (Sättigung +5,
   Belichtung −5 auf der CapCut-Skala, gegen einen echten Export kalibriert).
5. **Cut-Cockpit im Browser** geht auf: Timeline, Player, Feinschnitt.

**Dann korrigierst du von Hand nach** — ein bis zwei Minuten.

**Phase B — ein Klick auf "B-Roll erstellen".**

Das System zerlegt deinen Schnitt in Clips, zieht zu jedem den gesprochenen
Satz und beauftragt den `broll-ersteller`-Agenten. Für jeden Clip entsteht ein
eigener 5-Sekunden-Beat, der **automatisch auf die Clip-Länge beschleunigt**
wird — der Inhalt bleibt vollständig, nur das Tempo passt sich an. Erster und
letzter Clip bleiben frei (Hook und Call-to-Action).

Die fertigen Beats hängen sich selbst an ihre Clips. Wenn du danach einen Clip
trimmst, passt sich das B-Roll-Tempo von allein wieder an.

## Was du brauchst

- macOS oder Linux, Python 3.12, ffmpeg
- Claude Code
- Einen AssemblyAI-Schlüssel (kostenloses Konto, ein Video kostet Cent-Beträge)
- Für die B-Roll-Beats: den [broll-ersteller-Agenten](https://github.com/sebaskauf/broll-ersteller-agent)
  (der Setup-Prompt richtet ihn mit ein)

## Installation

Nimm den [SETUP-PROMPT.md](SETUP-PROMPT.md), kopier ihn komplett in Claude Code
und lass dich durch die Einrichtung führen. Claude prüft die Voraussetzungen,
baut die Umgebung, installiert beide Agenten und macht am Ende einen Testlauf
mit einem kurzen Video.

## Was NICHT drin ist

**Captions.** Die Untertitel-Funktion ist bewusst nicht Teil dieser Fassung —
sie wird separat weiterentwickelt. Wer Captions will, macht sie nach dem Export
in CapCut; das dauert eine Minute und sieht besser aus als alles, was das
System aktuell erzeugen würde.

**Automatisches Hochladen.** Das System endet beim fertigen MP4.

## Verwandte Systeme

- [claude-video-cutter](https://github.com/sebaskauf/claude-video-cutter) — dasselbe
  für Longform (20-90 Minuten)
- [broll-ersteller-agent](https://github.com/sebaskauf/broll-ersteller-agent) — der
  Agent, der die Beats baut

## Lizenz

MIT
