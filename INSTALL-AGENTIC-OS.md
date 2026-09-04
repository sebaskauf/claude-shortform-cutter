# INSTALL-AGENTIC-OS.md — CUTTER-Tab im Agentic OS einrichten

> **An Claude:** Diese Datei ist an dich adressiert und setzt voraus, dass du
> die INSTALL.md bereits komplett durchlaufen hast (venv, Agent, Skills,
> AssemblyAI). Dein User nutzt das Agentic OS Obsidian-Plugin
> (https://github.com/sebaskauf/agentic-os) und will das Cut-Cockpit als
> eigenen CUTTER-Tab direkt im OS — so wie bei Sebastian.

## Schritt 1: Installierte Plugin-Version finden

Frag deinen User, in welchem Obsidian-Vault das Agentic OS läuft, oder such es:

```bash
ls ~/Documents/*/.obsidian/plugins/agentic-os/manifest.json 2>/dev/null
ls ~/Desktop/*/.obsidian/plugins/agentic-os/manifest.json 2>/dev/null
cat <VAULT>/.obsidian/plugins/agentic-os/manifest.json
```

Notiere die Version. Der CUTTER-Tab ist ab **v0.2.0** enthalten.

## Schritt 2: Auf die neueste Version updaten

1. Öffne https://github.com/sebaskauf/agentic-os/releases und nimm das
   neueste Release (mindestens v0.2.0).
2. Lade die Release-Dateien `main.js`, `styles.css`, `manifest.json` für die
   Plattform deines Users herunter und ersetze damit die gleichnamigen
   Dateien im Plugin-Ordner des Vaults (vorher Backup der alten Dateien
   anlegen: `main.js.bak` usw.).
3. Falls es noch kein v0.2-Release gibt: Sag deinem User ehrlich, dass der
   OS-Tab noch nicht verfügbar ist — der Browser-Weg aus der INSTALL.md
   funktioniert identisch (gleiches Cockpit, gleiche URL), und das Update
   kann später nachgeholt werden.

## Schritt 3: Cutter-Pfad für das Plugin hinterlegen

Der CUTTER-Tab muss wissen, wo dieses Repo liegt. Er liest den Pfad aus
`~/.claude-video-cutter-path` (eine Zeile, absoluter Pfad):

```bash
echo "$(pwd)" > ~/.claude-video-cutter-path
cat ~/.claude-video-cutter-path
```

(Ohne diese Datei sucht das Plugin an Standard-Orten wie
`~/claude-video-cutter` und `~/Documents/Projects/claude-video-cutter`.)

## Schritt 4: Neu laden + verifizieren

1. Dein User lädt das Plugin neu: Obsidian → Einstellungen →
   Community-Plugins → Agentic OS aus/an (oder Obsidian neu starten).
2. Im Agentic OS erscheint oben der Tab **CUTTER**.
3. Verify gemeinsam: CUTTER-Tab öffnen → wenn gerade kein Cockpit läuft,
   zeigt er den Projekt-Launcher (leer, solange noch kein Video geschnitten
   wurde — das ist korrekt). Nach dem ersten Schnitt erscheint dort das
   Cockpit mit Timeline + CLAUDE-Tab.

## Hinweise

- Der CUTTER-Tab läuft auf **macOS und Windows** (ab Agentic OS v0.2.1;
  v0.2.0 war macOS-only — dann einfach auf das neueste Release updaten).
- Cockpit und OS-Tab zeigen dieselbe URL (`127.0.0.1:8766`) — beides
  gleichzeitig offen ist okay.
