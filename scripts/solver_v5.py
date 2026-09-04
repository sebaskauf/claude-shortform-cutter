#!/usr/bin/env python3
"""V5 Phase 3: Energie-Minimum-Solver. Ersetzt die naive Transkript-Gap-Mitte
aus cut.py durch GEMESSENE Schnittpunkte aus der Waveform.

Prinzip: Agent liefert cut_word_ids (WAS). Der Solver bestimmt WO:
- Out-Kante  = echtes Sprach-Ende (speech_offset) der Pause nach dem letzten
  Keep-Wort + trail-Padding, geclampt in die Pause.
- In-Kante   = echter Sprach-Beginn (speech_onset) der Pause vor dem ersten
  Keep-Wort - lead-Padding, geclampt in die Pause.
- Findet sich KEINE echte Pause (>=min_pause) an einer Kante -> Kante wird
  NICHT geschnitten sondern geflaggt (Graufall) + Fallback auf Aligner-Zeit.
- Dead-Air: Pausen > compress_s INNERHALB eines Keep-Runs werden auf
  beat_s gekuerzt (Split in zwei Segmente mit gemessenen Kanten).

Usage: solver_v5.py <audio48k.wav> <words.json> <decisions.json> <outdir>
Output: <outdir>/segments_v5.json (Segmente + Metriken + Flags)
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_measure import AudioMap

LEAD_S, TRAIL_S = 0.10, 0.15
MIN_PAUSE_S = 0.15

def main():
    audio_path, words_path, dec_path, outdir = sys.argv[1:5]
    os.makedirs(outdir, exist_ok=True)
    words = json.load(open(words_path))
    dec = json.load(open(dec_path))
    compress_s = dec.get("compress_pauses_over", 3.0)
    beat_s = dec.get("dead_air_beat", 0.5)

    am = AudioMap(audio_path, min_pause_s=MIN_PAUSE_S)
    print(f"[solver] Otsu {am.thr:.1f} dB, {len(am.pauses)} Pausen", flush=True)

    idx = {w["id"]: i for i, w in enumerate(words)}
    n = len(words)
    removed = [False] * n
    for wid in dec.get("cut_word_ids", []):
        if wid in idx: removed[idx[wid]] = True

    # Keep-Runs bilden, an Dead-Air > compress_s splitten
    runs, cur, prev = [], None, None
    for i in range(n):
        if removed[i]:
            if cur: runs.append(cur); cur = None
            prev = None; continue
        if cur is None:
            cur, prev = [i, i], i
        else:
            if words[i]["start"] - words[prev]["end"] > compress_s:
                runs.append(cur); cur = [i, i]
            else:
                cur[1] = i
            prev = i
    if cur: runs.append(cur)

    segs, flags = [], []
    for (a, b) in runs:
        wa, wb = words[a], words[b]
        seg = {"in": None, "out": None, "first_word": wa["text"], "last_word": wb["text"],
               "first_id": wa["id"], "last_id": wb["id"], "metrics": {}, "flags": []}

        # IN-Kante — Pause muss ZWISCHEN letztem Cut-Wort und erstem Keep-Wort liegen,
        # sonst greift sie zu weit zurueck und Cut-Material blutet rein (QA-Befund Naht 0)
        if a == 0:
            seg["in"] = max(0.0, wa["start"] - 0.25)
        else:
            w_prev_end = words[a - 1]["end"]
            p_in = am.pause_before(wa["start"])
            if p_in and p_in["end"] < w_prev_end - 0.15:
                p_in = None   # Pause liegt noch VOR dem letzten Cut-Wort -> unbrauchbar
            if p_in:
                onset = am.speech_onset(p_in)
                seg["in"] = max(onset - LEAD_S, p_in["start"] + 0.01)
                seg["metrics"]["in_pause_dur"] = round(p_in["dur"], 3)
                seg["metrics"]["in_moved_ms"] = round((seg["in"] - wa["start"]) * 1000, 1)
                seg["metrics"]["in_onset"] = round(onset, 4)
            else:
                seg["in"] = max(0.0, wa["start"] - 0.05)
                seg["flags"].append("in_no_pause")

        # OUT-Kante — Pause muss VOR dem naechsten Cut-Wort beginnen,
        # sonst greift sie zu weit vor und Cut-Material blutet rein (QA-Befund Naht 14)
        p_out = am.pause_after(wb["end"])
        if b < n - 1 and p_out and p_out["start"] > words[b + 1]["start"] + 0.15:
            p_out = None   # Pause liegt erst NACH dem naechsten Cut-Wort -> unbrauchbar
        if b == n - 1:
            seg["out"] = wb["end"] + 0.30
        elif p_out:
            offset = am.speech_offset(p_out)
            nxt_onset = am.speech_onset(p_out)
            seg["out"] = min(offset + TRAIL_S, p_out["end"] - 0.01,
                             nxt_onset - 0.05 if nxt_onset > offset else p_out["end"] - 0.01)
            seg["out"] = max(seg["out"], offset + 0.02)   # nie vor dem echten Sprach-Ende
            seg["metrics"]["out_pause_dur"] = round(p_out["dur"], 3)
            seg["metrics"]["out_moved_ms"] = round((seg["out"] - wb["end"]) * 1000, 1)
            seg["metrics"]["out_offset"] = round(offset, 4)
        else:
            seg["out"] = wb["end"] + 0.05
            seg["flags"].append("out_no_pause")

        if seg["out"] <= seg["in"]:
            seg["flags"].append("degenerate")
        segs.append(seg)
        if seg["flags"]:
            flags.append({"first_id": wa["id"], "last_id": wb["id"], "flags": seg["flags"],
                          "in": round(seg["in"], 3), "out": round(seg["out"], 3)})

    # Ueberlappungen mergen (defensive)
    segs = [s for s in segs if "degenerate" not in s["flags"]]
    merged = []
    for s in segs:
        if merged and s["in"] <= merged[-1]["out"] + 0.02:
            merged[-1]["out"] = max(merged[-1]["out"], s["out"])
            merged[-1]["last_word"] = s["last_word"]; merged[-1]["last_id"] = s["last_id"]
            merged[-1]["flags"] = sorted(set(merged[-1]["flags"] + s["flags"] + ["merged"]))
        else:
            merged.append(s)

    keep = sum(s["out"] - s["in"] for s in merged)
    total = words[-1]["end"]
    out = {"segments": [{**s, "in": round(s["in"], 4), "out": round(s["out"], 4)} for s in merged],
           "flags": flags, "keep_s": round(keep, 1), "total_s": round(total, 1),
           "n_segments": len(merged), "n_cut_edges": max(0, 2 * len(merged) - 2),
           "params": {"lead": LEAD_S, "trail": TRAIL_S, "min_pause": MIN_PAUSE_S,
                      "compress_over": compress_s, "beat": beat_s}}
    json.dump(out, open(os.path.join(outdir, "segments_v5.json"), "w"),
              ensure_ascii=False, indent=1)
    print(f"[solver] {len(merged)} Segmente, {keep:.0f}s/{total:.0f}s behalten, "
          f"{len(flags)} Graufall-Kanten -> segments_v5.json", flush=True)

if __name__ == "__main__":
    main()
