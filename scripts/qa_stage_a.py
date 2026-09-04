#!/usr/bin/env python3
"""V5 Phase 4, Stufe A: deterministischer Per-Cut-Verify (alle Schnittstellen).

Fuer jede Naht zwischen zwei Keep-Segmenten (v2 — Seiten GETRENNT geprueft,
large-v3 als Pruefer, Leak-Check nur auf nicht-erwartete Woerter):
1. PRE-Seite  [out-1,5s .. out]  und POST-Seite [in .. in+1,5s] einzeln
   re-transkribieren (kein Splice-Artefakt im Pruefer)
2. Grenzwort-Check: letztes Keep-Wort vor der Naht / erstes danach muss
   fuzzy im Gehoerten auftauchen (Detektor fuer abgeschnittene Woerter)
3. Leak-Check: Woerter im Gehoerten, die NICHT im Erwarteten vorkommen,
   aber im geschnittenen Material -> Cut-Material blutet rein
4. Energie-Assertion: 30ms beidseits der Klebestelle muss Stille sein

Output: qa_stage_a.json (pro Naht: pass/fail + Gruende).

Usage: qa_stage_a.py <audio48k.wav> <words.json> <decisions.json> <segments_v5.json> <outdir>
"""
import sys, os, json, tempfile
import numpy as np
import soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_measure import AudioMap
from rapidfuzz import fuzz

CTX_S = 1.5
BOUNDARY_SIM_MIN = 70   # Grenzwort muss so aehnlich im Gehoerten stecken
SIDE_SIM_MIN = 50       # Seiten-Text grob aehnlich (large-v3 paraphrasiert wenig)

def norm(s):
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()

def toks(s):
    return [t for t in norm(s).split() if len(t) > 2]

def words_in_range(words, t0, t1):
    return [w for w in words if w["start"] < t1 and w["end"] > t0]

def main():
    audio_path, words_path, dec_path, segs_path, outdir = sys.argv[1:6]
    limit = int(sys.argv[6]) if len(sys.argv) > 6 else 0
    os.makedirs(outdir, exist_ok=True)
    words = json.load(open(words_path))
    dec = json.load(open(dec_path))
    S = json.load(open(segs_path))["segments"]
    cut_ids = set(dec.get("cut_word_ids", []))

    am = AudioMap(audio_path)
    x, sr = am.x, am.sr

    print("[qaA] lade faster-whisper large-v3 (de, int8) ...", flush=True)
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="cpu", compute_type="int8", cpu_threads=8)

    def hear(clip):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            sf.write(f.name, clip.astype(np.float32), sr)
            segs, _ = model.transcribe(f.name, language="de", beam_size=1,
                                       condition_on_previous_text=False,
                                       no_repeat_ngram_size=3)
            heard = " ".join(s.text for s in segs).strip()
            os.unlink(f.name)
        return heard

    # Checkpoint-Resume: bei Kill geht kein Fortschritt mehr verloren (13.07.)
    partial_path = os.path.join(outdir, "qa_stage_a.partial.json")
    done = {}
    if os.path.exists(partial_path):
        for r in json.load(open(partial_path)):
            k = r["joint"]
            # nur uebernehmen wenn die Naht-Zeiten noch zu den Segmenten passen
            if k + 1 < len(S) and abs(S[k]["out"] - r["out"]) < 0.002 and abs(S[k + 1]["in"] - r["in"]) < 0.002:
                done[k] = r
        print(f"[qaA] Resume: {len(done)} Naehte aus Checkpoint uebernommen", flush=True)

    results = []
    n_joints = min(len(S) - 1, limit) if limit else len(S) - 1
    for k in range(n_joints):
        if k in done:
            results.append(done[k]); continue
        out_t, in_t = S[k]["out"], S[k + 1]["in"]
        pre = x[max(0, int((out_t - CTX_S) * sr)):int(out_t * sr)]
        post = x[int(in_t * sr):min(len(x), int((in_t + CTX_S) * sr))]
        heard_pre, heard_post = hear(pre), hear(post)
        heard_all = heard_pre + " " + heard_post

        exp_pre = [w for w in words_in_range(words, out_t - CTX_S, out_t)
                   if w["id"] not in cut_ids]
        exp_post = [w for w in words_in_range(words, in_t, in_t + CTX_S)
                    if w["id"] not in cut_ids]
        gap_cut = [w for w in words_in_range(words, out_t - 0.2, in_t + 0.2)
                   if w["id"] in cut_ids]

        fails = []
        # Grenzwoerter: das letzte/erste Keep-Wort MUSS hoerbar sein
        b_pre = S[k]["last_word"]; b_post = S[k + 1]["first_word"]
        sim_bp = fuzz.partial_ratio(norm(b_pre), norm(heard_pre)) if norm(b_pre) else 100
        sim_bq = fuzz.partial_ratio(norm(b_post), norm(heard_post)) if norm(b_post) else 100
        if sim_bp < BOUNDARY_SIM_MIN: fails.append(f"boundary_pre_clipped({b_pre}|{sim_bp:.0f})")
        if sim_bq < BOUNDARY_SIM_MIN: fails.append(f"boundary_post_clipped({b_post}|{sim_bq:.0f})")

        # Seiten-Aehnlichkeit (grob)
        sp = fuzz.token_sort_ratio(norm(" ".join(w["text"] for w in exp_pre)), norm(heard_pre))
        sq = fuzz.token_sort_ratio(norm(" ".join(w["text"] for w in exp_post)), norm(heard_post))
        if exp_pre and sp < SIDE_SIM_MIN: fails.append(f"pre_text_mismatch({sp:.0f})")
        if exp_post and sq < SIDE_SIM_MIN: fails.append(f"post_text_mismatch({sq:.0f})")

        # Leak: gehoerte Tokens, die nicht erwartet sind, aber im Cut-Material
        exp_tok = set(toks(" ".join(w["text"] for w in exp_pre + exp_post)))
        cut_tok = set(toks(" ".join(w["text"] for w in gap_cut)))
        extra = [t for t in toks(heard_all) if t not in exp_tok]
        leaked = [t for t in extra if any(fuzz.ratio(t, c) > 85 for c in cut_tok - exp_tok)]
        if len(leaked) >= 2: fails.append(f"cut_text_leaked({','.join(leaked[:4])})")

        # Energie an der Klebestelle
        j0, j1 = int(0.03 * sr), int(0.03 * sr)
        edge = np.concatenate([pre[-j0:] if len(pre) >= j0 else pre,
                               post[:j1] if len(post) >= j1 else post])
        edge_db = 20 * np.log10(np.sqrt(np.mean(edge.astype(np.float64) ** 2)) + 1e-10)
        if edge_db >= am.thr + 6: fails.append(f"joint_not_silent({edge_db:.0f}dB)")

        results.append({"joint": k, "out": round(out_t, 3), "in": round(in_t, 3),
                        "expected_pre": " ".join(w["text"] for w in exp_pre),
                        "expected_post": " ".join(w["text"] for w in exp_post),
                        "heard_pre": heard_pre, "heard_post": heard_post,
                        "cut_sample": " ".join(w["text"] for w in gap_cut[:10]),
                        "edge_db": round(float(edge_db), 1),
                        "pass": not fails, "fails": fails})
        if (k + 1) % 10 == 0:
            print(f"[qaA] {k+1}/{len(S)-1} Naehte geprueft", flush=True)
            json.dump(results, open(partial_path, "w"), ensure_ascii=False)

    n_fail = sum(1 for r in results if not r["pass"])
    json.dump(results, open(os.path.join(outdir, "qa_stage_a.json"), "w"),
              ensure_ascii=False, indent=1)
    if not limit and os.path.exists(partial_path):
        os.remove(partial_path)
    print(f"[qaA] FERTIG: {len(results)} Naehte, {len(results)-n_fail} PASS, {n_fail} FAIL "
          f"-> qa_stage_a.json", flush=True)

if __name__ == "__main__":
    main()
