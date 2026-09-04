#!/usr/bin/env python3
"""Clip-Register fuer das Shortform-System (P2, 03.09.2026).

Leitet aus dem AKTUELLEN Schnitt ein Register aller Clips ab: Quellzeiten,
Cut-Zeiten, Dauer, Volltext, Rolle. Das ist die Grundlage fuer den
B-Roll-Auftrag (ein B-Roll pro Body-Clip) und fuer den Farb-Look.

Rollen (der Vorgabe 03.09.):
  erster Clip  = hook  -> KEIN B-Roll, dort kommt das First-Frame-Image
  letzter Clip = cta   -> KEIN B-Roll, dort kommt nur Text
  Rest         = body  -> je ein B-Roll, per Speed-Fit auf die Clip-Laenge

Achsen-Disziplin (Doktrin): timeline_clips fuehrt QUELLZEITEN (in/out des
Rohvideos). Die Cut-Achse ergibt sich aus den kumulierten Clip-Dauern und
ist die Achse, auf der B-Roll-Slots und Captions liegen.

Aufruf:
  clip_register.py <workdir> [--verify]
"""
import json, os, sys, datetime

def load(workdir, name, default=None):
    p = os.path.join(workdir, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build(workdir):
    ovr = load(workdir, "cockpit_overrides.json", {}) or {}
    clips = ovr.get("timeline_clips") or []
    if not clips:
        raise SystemExit("[clip_register] keine timeline_clips — erst schneiden")
    words = load(workdir, "words_aai.json", []) or []

    # Doktrin-Learning 1 (28.08.): die Re-Transkription des CUTS ist der
    # Praezisions-Anker. AAI verschluckt am Quellmaterial nachweislich Takes
    # (Lauf 1: zwei komplette CTA-Takes). Der Clip-Text bestimmt spaeter die
    # B-Roll-Metapher — also nehmen wir, was im Schnitt wirklich zu hoeren ist.
    cutw, cut_src = None, None
    for name in ("cut_words.json", "cc_words.json"):
        cand = load(workdir, name)
        if cand:
            cutw, cut_src = cand, name
            break

    out, t = [], 0.0
    last = len(clips) - 1
    for i, c in enumerate(clips):
        a, b = float(c["in"]), float(c["out"])
        dur = round(b - a, 4)
        if dur <= 0:
            raise SystemExit(f"[clip_register] Clip {i} ({c.get('id')}) hat Dauer {dur}")
        # Woerter, deren MITTE im Clip-Fenster liegt (Quellachse)
        ws = [w for w in words
              if a <= (float(w["start"]) + float(w["end"])) / 2.0 <= b]
        text_aai = " ".join(w["text"] for w in ws).strip()
        role = "hook" if i == 0 else ("cta" if i == last else "body")
        out.append({
            "idx": i,
            "id": c.get("id"),
            "role": role,
            "src_in": round(a, 4),
            "src_out": round(b, 4),
            "dur": dur,
            "cut_start": round(t, 4),
            "cut_end": round(t + dur, 4),
            "word_count": len(ws),
            "text": text_aai,          # wird unten durch den Cut-Text ersetzt
            "text_aai": text_aai,      # Quellachse, nur zum Vergleich
            "text_source": "aai",
            "sat": c.get("sat", 0),
            "exp": c.get("exp", 0),
        })
        t += dur

    # Cut-Text uebernehmen, wenn die Re-Transkription zum aktuellen Schnitt passt
    if cutw:
        ende = max(float(w["end"]) for w in cutw)
        frisch = abs(ende - t) <= max(1.0, t * 0.05)
        if frisch:
            # exklusive Zuordnung: jedes Wort gehoert genau EINEM Clip
            # (Mitte im Fenster). Sonst tauchen Nahtwoerter doppelt auf und
            # verfaelschen die Metapher des Nachbar-Beats.
            for c in out:
                letzter = c["idx"] == len(out) - 1
                ws = [w["text"] for w in cutw
                      if c["cut_start"] <= (float(w["start"]) + float(w["end"])) / 2.0
                      < (c["cut_end"] + 1e9 if letzter else c["cut_end"])]
                if ws:
                    c["text"] = " ".join(ws).strip()
                    c["text_source"] = cut_src
        else:
            print(f"[clip_register] ⚠ {cut_src} passt nicht zum aktuellen Schnitt "
                  f"(endet {ende:.1f}s, Schnitt {t:.1f}s) — Text kommt aus AAI. "
                  f"Nach dem naechsten Cut neu re-transkribieren.", flush=True)

    doc = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "workdir": os.path.basename(os.path.abspath(workdir)),
        "clip_count": len(out),
        "body_count": sum(1 for c in out if c["role"] == "body"),
        "total_dur": round(t, 4),
        "clips": out,
    }
    return doc


def verify(workdir, doc):
    """Harte Gegenproben. Gibt (ok, zeilen) zurueck."""
    ok, lines = True, []
    clips = doc["clips"]

    # 1) Cut-Achse lueckenlos und monoton
    for i, c in enumerate(clips):
        if i and abs(c["cut_start"] - clips[i - 1]["cut_end"]) > 1e-6:
            ok = False
            lines.append(f"  FEHLER Luecke auf der Cut-Achse bei Clip {i}")
    exp_total = round(sum(c["dur"] for c in clips), 4)
    if abs(exp_total - doc["total_dur"]) > 1e-6:
        ok = False
        lines.append(f"  FEHLER Summe {exp_total} != total_dur {doc['total_dur']}")
    else:
        lines.append(f"  OK Cut-Achse lueckenlos, Gesamtdauer {doc['total_dur']:.2f}s")

    # 2) Rollen
    roles = [c["role"] for c in clips]
    if roles[0] != "hook" or roles[-1] != "cta":
        ok = False
        lines.append(f"  FEHLER Rollen falsch: {roles[0]} .. {roles[-1]}")
    elif any(r != "body" for r in roles[1:-1]):
        ok = False
        lines.append("  FEHLER Rolle in der Mitte ist nicht body")
    else:
        lines.append(f"  OK Rollen: hook + {doc['body_count']}x body + cta")

    # 3) Jeder Clip hat Text
    leer = [c["idx"] for c in clips if not c["text"]]
    if leer:
        ok = False
        lines.append(f"  FEHLER Clips ohne Text: {leer}")
    else:
        lines.append(f"  OK alle {len(clips)} Clips haben Text")

    # 4) Gegenprobe gegen die Re-Transkription des Cuts (falls vorhanden)
    cutw = None
    for name in ("cut_words.json", "cc_words.json"):
        cutw = load(workdir, name)
        if cutw:
            src = name
            break
    if not cutw:
        lines.append("  (keine Cut-Re-Transkription im Workdir — Textprobe uebersprungen)")
        return ok, lines

    try:
        from rapidfuzz import fuzz
    except ImportError:
        lines.append("  (rapidfuzz fehlt — Textprobe uebersprungen)")
        return ok, lines

    def norm(s):
        return "".join(ch.lower() for ch in s if ch.isalnum() or ch == " ")

    treffer = 0
    for c in clips:
        # Woerter der Re-Transkription im Cut-Fenster dieses Clips
        ws = [w["text"] for w in cutw
              if c["cut_start"] - 0.15 <= (float(w["start"]) + float(w["end"])) / 2.0 <= c["cut_end"] + 0.15]
        if not ws:
            continue
        score = fuzz.token_set_ratio(norm(" ".join(ws)), norm(c.get("text_aai") or c["text"]))
        if score >= 70:
            treffer += 1
        else:
            lines.append(f"  HINWEIS Clip {c['idx']} ({c['role']}): AAI weicht vom Cut ab "
                         f"(score {score:.0f}) — Cut-Text wird verwendet")
            lines.append(f"          AAI-Quelle: {(c.get('text_aai') or '')[:70]}")
            lines.append(f"          Cut-ASR:    {' '.join(ws)[:70]}")
    q = sum(1 for c in clips if c.get("text_source") not in (None, "aai"))
    lines.append(f"  Textquelle: {q}/{len(clips)} Clips aus der Cut-Re-Transkription")
    lines.append(f"  {treffer}/{len(clips)} Clips decken sich mit AAI (Schwelle 70)")
    return ok, lines


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    workdir = os.path.abspath(sys.argv[1])
    doc = build(workdir)
    p = os.path.join(workdir, "clips.json")
    tmp = p + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    print(f"[clip_register] {doc['clip_count']} Clips "
          f"(hook + {doc['body_count']} body + cta), {doc['total_dur']:.2f}s -> clips.json")

    if "--verify" in sys.argv:
        ok, lines = verify(workdir, doc)
        print("[clip_register] Verifikation:")
        for l in lines:
            print(l)
        if not ok:
            raise SystemExit("[clip_register] VERIFIKATION FEHLGESCHLAGEN")
        print("[clip_register] Verifikation bestanden")


if __name__ == "__main__":
    main()
