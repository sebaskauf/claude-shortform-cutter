#!/usr/bin/env python3
"""V5 Phase 4: deterministischer Auto-Repair auf Basis der QA-Stufe-A-Fails.

Regeln (konservativ, nie raten):
- boundary_post_clipped: MIKRO-SCAN im 0,8s-Fenster VOR der IN-Kante mit lokalem,
  empfindlicherem Schwellwert (leise Funktionswoerter wie "Und" liegen unter dem
  globalen Otsu-Schwellwert — Spektrogramm-Befund Naht 10). Findet er einen
  Sprach-Run >=50ms, wird die Kante bis knapp davor erweitert — nur wenn der
  Bereich laut Wort-Karte keine Cut-Woerter enthaelt.
- boundary_pre_clipped: symmetrisch NACH der OUT-Kante.
- joint_not_silent ALLEIN (Text-Checks gruen): WARN, keine Aenderung (Atmer im
  Pausenfenster, kein Fehler — Spektrogramm-Befund Naht 26).
- alles andere / Bedingung verletzt: GRAUFALL fuer Review-Liste.

Usage: qa_repair.py <audio48k> <words.json> <decisions.json> <segments.json> <qa.json> <out_segments.json>
"""
import sys, os, json
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_measure import AudioMap, rms_db

LEAD_S, TRAIL_S = 0.10, 0.15
SCAN_S = 0.8          # max. Erweiterung pro Repair
RUN_MIN_S = 0.05      # leiser Sprach-Run muss >=50ms sein
SENS_DB = 5.0         # Schwellwert = lokaler Floor + SENS_DB

def overlapping(words, t0, t1):
    return [w for w in words if w["start"] < t1 and w["end"] > t0]

def micro_runs(am, t0, t1):
    """Leise Sprach-Runs in [t0,t1] mit lokalem Floor+5dB, 5ms-RMS."""
    a = max(0, int(t0 * am.sr)); b = min(len(am.x), int(t1 * am.sr))
    if b - a < am.sr // 20: return []
    t5, db5, hop5 = rms_db(am.x[a:b], am.sr, win_ms=5.0, hop_ms=1.0)
    thr = np.percentile(db5, 10) + SENS_DB
    above = db5 > thr
    run_min = max(1, int(RUN_MIN_S / hop5))
    runs, i, n = [], 0, len(above)
    while i < n:
        if above[i]:
            j = i
            while j < n and above[j]: j += 1
            if (j - i) >= run_min:
                runs.append((a / am.sr + t5[i], a / am.sr + t5[j - 1] + 0.005))
            i = j
        else:
            i += 1
    return runs

def main():
    audio_path, words_path, dec_path, segs_path, qa_path, out_path = sys.argv[1:7]
    words = json.load(open(words_path))
    cut_ids = set(json.load(open(dec_path)).get("cut_word_ids", []))
    doc = json.load(open(segs_path))
    S = doc["segments"]
    QA = json.load(open(qa_path))

    am = AudioMap(audio_path)
    repairs, warns, grau = [], [], []

    for r in QA:
        if r["pass"]: continue
        k = r["joint"]
        kinds = {f.split("(")[0] for f in r["fails"]}

        if kinds == {"joint_not_silent"}:
            warns.append({"joint": k, "note": "Atmer/Decay im Nahtfenster, Text-Checks gruen"})
            continue

        fixed = False
        if "boundary_post_clipped" in kinds:
            in_t = S[k + 1]["in"]
            runs = micro_runs(am, in_t - SCAN_S, in_t - 0.02)
            if runs:
                new_in = max(runs[0][0] - 0.08, in_t - SCAN_S)
                span = overlapping(words, new_in - 0.02, in_t)
                if not any(w["id"] in cut_ids for w in span):
                    S[k + 1]["in"] = round(new_in, 4)
                    S[k + 1].setdefault("flags", []).append("repaired_in")
                    repairs.append({"joint": k, "edge": "in", "old": in_t, "new": round(new_in, 4),
                                    "runs": [(round(a, 3), round(b, 3)) for a, b in runs]})
                    fixed = True

        if "boundary_pre_clipped" in kinds:
            out_t = S[k]["out"]
            runs = micro_runs(am, out_t + 0.02, out_t + SCAN_S)
            if runs:
                new_out = min(runs[-1][1] + 0.12, out_t + SCAN_S)
                span = overlapping(words, out_t, new_out + 0.02)
                if not any(w["id"] in cut_ids for w in span):
                    S[k]["out"] = round(new_out, 4)
                    S[k].setdefault("flags", []).append("repaired_out")
                    repairs.append({"joint": k, "edge": "out", "old": out_t, "new": round(new_out, 4),
                                    "runs": [(round(a, 3), round(b, 3)) for a, b in runs]})
                    fixed = True

        if not fixed and not kinds <= {"joint_not_silent"}:
            grau.append({"joint": k, "out": r["out"], "in": r["in"], "fails": r["fails"],
                         "heard_pre": r["heard_pre"][-60:], "heard_post": r["heard_post"][:60]})

    doc["segments"] = S
    doc["repair"] = {"repairs": repairs, "warns": warns, "graufaelle": grau}
    json.dump(doc, open(out_path, "w"), ensure_ascii=False, indent=1)
    print(f"[repair] {len(repairs)} Kanten repariert, {len(warns)} Warnungen (ok), "
          f"{len(grau)} Graufaelle -> {out_path}", flush=True)
    for rp in repairs:
        print(f"[repair]   Naht {rp['joint']} {rp['edge']}: {rp['old']} -> {rp['new']} "
              f"(Runs: {rp['runs']})", flush=True)
    for g in grau:
        print(f"[grau]    Naht {g['joint']}: {g['fails']}", flush=True)

if __name__ == "__main__":
    main()
