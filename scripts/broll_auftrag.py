#!/usr/bin/env python3
"""B-Roll-Auftrag aus dem finalen Schnitt (P4, 03.09.2026).

Der Ablauf: schneiden, von Hand nachkorrigieren, dann
"B-Roll erstellen". Erst dieser Klick erklaert die Clips fuer final — ab da
steht fest, wie viele Clips es gibt und wie lang jeder ist. Dieses Skript
macht daraus den Auftrag fuer den broll-ersteller-Agent.

Regeln:
  - genau EIN B-Roll pro Body-Clip
  - erster Clip (hook) bekommt keins -> dort das First-Frame-Image
  - letzter Clip (cta)  bekommt keins -> dort nur Text
  - jeder Beat wird 5,0 s lang gebaut und per Speed-Fit auf die Clip-Laenge
    gebracht. KEINE Speed-Grenze — Faktoren bis 3x sind normal und gewollt.

Aufruf:
  broll_auftrag.py <workdir> [--json]
"""
import json, os, subprocess, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BEAT_DUR = 5.0


def clips_lesen(workdir):
    """clips.json holen; wenn es fehlt oder veraltet ist, neu erzeugen."""
    subprocess.run([sys.executable, os.path.join(HERE, "clip_register.py"), workdir],
                   check=True, capture_output=True, text=True)
    with open(os.path.join(workdir, "clips.json"), encoding="utf-8") as f:
        return json.load(f)


def bauen(workdir):
    reg = clips_lesen(workdir)
    name = reg["workdir"]
    auftraege, uebersprungen = [], []
    nr = 0
    for c in reg["clips"]:
        if c["role"] != "body":
            uebersprungen.append({
                "clip_idx": c["idx"], "role": c["role"], "dur": c["dur"],
                "grund": ("First-Frame-Image statt B-Roll" if c["role"] == "hook"
                          else "nur Text (Call-to-Action)"),
                "text": c["text"],
            })
            continue
        nr += 1
        auftraege.append({
            "nr": nr,
            "clip_idx": c["idx"],
            "clip_id": c["id"],
            "clip_dur": c["dur"],
            "cut_start": c["cut_start"],
            "cut_end": c["cut_end"],
            "speed_fit": round(BEAT_DUR / c["dur"], 4),
            "text": c["text"],
            "out": f"broll/{name}-{nr:02d}.mp4",
            "status": "offen",
        })
    return {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "workdir": name,
        "beat_dur": BEAT_DUR,
        "clip_count": reg["clip_count"],
        "auftrag_count": len(auftraege),
        "auftraege": auftraege,
        "uebersprungen": uebersprungen,
    }


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    workdir = os.path.abspath(sys.argv[1])
    doc = bauen(workdir)
    p = os.path.join(workdir, "broll_auftrag.json")
    tmp = p + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    os.makedirs(os.path.join(workdir, "broll"), exist_ok=True)

    if "--json" in sys.argv:
        print(json.dumps(doc, ensure_ascii=False))
        return
    print(f"[broll_auftrag] {doc['auftrag_count']} Auftraege aus "
          f"{doc['clip_count']} Clips -> broll_auftrag.json")
    for a in doc["auftraege"]:
        print(f"  {a['nr']:>2}. Clip {a['clip_idx']:>2}  {a['clip_dur']:>5.2f}s  "
              f"Speed {a['speed_fit']:>4.2f}x  {a['text'][:52]}")
    for u in doc["uebersprungen"]:
        print(f"   -  Clip {u['clip_idx']:>2}  {u['dur']:>5.2f}s  "
              f"[{u['role']}] {u['grund']}")


if __name__ == "__main__":
    main()
