#!/usr/bin/env python3
"""Auftragstext fuer den broll-ersteller-Agent (P5, 03.09.2026).

Arbeitsteilung: der shortform-cutter macht alles, der
broll-ersteller uebernimmt NUR das Bauen der Beats — beauftragt durch den
Knopf "B-Roll erstellen" im Cockpit.

Der Brief enthaelt NUR das Projektspezifische: die Skript-Teile der Clips
und wohin die Dateien sollen. Wie ein Beat gebaut wird, steht im Agent-File
des broll-ersteller — das muss ihm niemand nochmal erklaeren.

Erste Fassung (03.09.) war mit Doktrin-Wiederholungen ueberladen (3805
Zeichen fuer 12 Beats). der Nutzer am 04.09.: der Agent soll es machen "wie
er es kennt, wie es bisher laeuft". Sein eigener Satz entstand in 10 Minuten
— ohne jede Zusatzanweisung von uns.

Aufruf:
  broll_brief.py <workdir> [--offen-nur]
"""
import json, os, sys

def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    workdir = os.path.abspath(sys.argv[1])
    nur_offen = "--offen-nur" in sys.argv
    p = os.path.join(workdir, "broll_auftrag.json")
    if not os.path.exists(p):
        raise SystemExit("[broll_brief] broll_auftrag.json fehlt — erst 'B-Roll erstellen' druecken")
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)

    ziel = os.path.join(workdir, "broll")
    auftraege = []
    for a in doc["auftraege"]:
        fertig = os.path.exists(os.path.join(workdir, a["out"]))
        if nur_offen and fertig:
            continue
        auftraege.append((a, fertig))

    if not auftraege:
        print("[broll_brief] nichts offen — alle Beats liegen schon in broll/")
        return

    zeilen = []
    zeilen.append(f"{len(auftraege)} B-Roll-Beats fuer das Reel '{doc['workdir']}' "
                  f"— einer je Clip, aus dem was im Clip gesagt wird.")
    zeilen.append("")
    zeilen.append(f"Zielordner: {ziel}")
    zeilen.append("")
    for a, fertig in auftraege:
        zeilen.append(f"{a['nr']:>2}. {a['out']}")
        zeilen.append(f"    \"{a['text']}\"")
        zeilen.append("")
    uebersprungen = doc.get("uebersprungen") or []
    if uebersprungen:
        zeilen.append("NICHT bauen (der Regel):")
        for u in uebersprungen:
            zeilen.append(f"  - Clip {u['clip_idx']} [{u['role']}]: {u['grund']}")
    print("\n".join(zeilen))


if __name__ == "__main__":
    main()
