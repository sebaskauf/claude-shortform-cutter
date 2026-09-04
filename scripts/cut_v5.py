#!/usr/bin/env python3
"""V5 Phase 5: Render aus segments_v5.json (Solver-Ausgabe, gemessene Kanten).

⚠️ UMGESTELLT AUF DEN SPLIT-PFAD (28.08.2026, V6-Vorfall)
Der alte concat-Pfad hier schnitt mit den ROHEN in/out und liess bewusst die
Source-fps stehen ("V4 erzwang -r 30 auf 60fps-Material"). Bei VFR-Quellen, die
sich im Container als 60fps deklarieren (QuickTime-Fusionen!), erzeugte das:
  - effektiv 29,956 statt 30,000 fps
  - 25 fehlende Bilder (27357 statt 27382)
  - PTS-Raster-Abweichung 1374 ms  -> verify_render FAIL
Ein Player, der die deklarierte Rate statt der echten Zeitstempel nimmt, spielt
Bild und Ton dann ueber 15 min um ~1,4 s versetzt ab. Genau das hat Sebastian
an V6 gesehen ("Der Ton war KOMPLETT versetzt vom Video").

Dieselbe Klasse Fehler war fuer rerender.py schon am 15.08. gefixt worden
(Split-Pfad: apply_jcut_edges rastert die Bilddauern auf exakte 30fps-
Vielfache, Bild/Ton als getrennte Ketten, ffconcat mit expliziten duration-
Zeilen) — cut_v5.py wurde damals nur nicht nachgezogen. Jetzt nachgeholt:
dieses Skript delegiert an denselben bewiesenen Pfad.
Messung V6: PTS-Raster 0,00 ms PASS, A/V-Dauer 0,3 ms, Naht-Sync Median -7,4 ms.

- --mode proxy  : 1080p, libx264 (schnelle Review-Runde)
- --mode final  : Source-Aufloesung, hevc_videotoolbox

Usage: cut_v5.py <src_video> <segments_v5.json> <out.mp4> [proxy|final]
"""
import sys, os, json, subprocess, tempfile, importlib.util

BATCH = 25
FADE_S = 0.012

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:]); raise SystemExit("ffmpeg failed")
    return r

def _render_via_split(src, segs_path, out_mp4, mode):
    """Delegiert an rerender.py::render_segments (Split-Pfad).

    Ohne neuen Solver-Lauf: die Segmente kommen genau so, wie sie in
    segments_v5*.json stehen — manuelle Kantenkorrekturen bleiben erhalten.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("rr", os.path.join(here, "rerender.py"))
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)

    segs = [dict(s) for s in json.load(open(segs_path))["segments"] if s["out"] > s["in"]]
    for s in segs:
        s.setdefault("gain_db", 0.0)
        s["jcut"] = 0.0
    rr.apply_jcut_edges(segs)          # setzt v_in/v_out/v_start -> Split-Pfad aktiv
    frames = sum(max(1, round((s["v_out"] - s["v_in"]) * rr.FPS)) for s in segs)
    print(f"[cutv5] Split-Pfad: {len(segs)} Segmente, Soll {frames} Bilder "
          f"= {frames / rr.FPS:.3f}s bei {rr.FPS:g} fps", flush=True)
    workdir = os.path.dirname(os.path.abspath(segs_path))
    rr.render_segments(segs, src, out_mp4, mode, [], workdir)
    print(f"[cutv5] FERTIG -> {out_mp4}", flush=True)

def main():
    src, segs_path, out_mp4 = sys.argv[1], sys.argv[2], sys.argv[3]
    mode = sys.argv[4] if len(sys.argv) > 4 else "proxy"

    # Standardweg seit 28.08.2026: Split-Pfad (siehe Modulkopf). Der alte
    # concat-Pfad darunter bleibt nur als Notausgang erreichbar.
    if os.environ.get("CUTV5_LEGACY_CONCAT") != "1":
        return _render_via_split(src, segs_path, out_mp4, mode)

    print("[cutv5] WARNUNG: Legacy-concat-Pfad aktiv — erzeugt bei VFR-Quellen "
          "ein kaputtes PTS-Raster (verify_render FAIL).", flush=True)
    S = json.load(open(segs_path))["segments"]
    segs = [(s["in"], s["out"]) for s in S]

    if mode == "proxy":
        vf_extra = ",scale=-2:1080"
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    else:
        vf_extra = ""
        venc = ["-c:v", "hevc_videotoolbox", "-q:v", "55", "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]

    keep = sum(e - s for s, e in segs)
    print(f"[cutv5] {len(segs)} Segmente, {keep:.0f}s, mode={mode}", flush=True)

    tmp = tempfile.mkdtemp(prefix="cutv5_"); batches = []
    for bi in range(0, len(segs), BATCH):
        chunk = segs[bi:bi + BATCH]; parts = ""; ci = ""
        for k, (s, e) in enumerate(chunk):
            fo = max(0.0, (e - s) - FADE_S)
            parts += (f"[0:v]trim={s:.4f}:{e:.4f},setpts=PTS-STARTPTS{vf_extra}[v{k}];"
                      f"[0:a]atrim={s:.4f}:{e:.4f},asetpts=PTS-STARTPTS,"
                      f"afade=t=in:st=0:d={FADE_S},afade=t=out:st={fo:.4f}:d={FADE_S}[a{k}];")
            ci += f"[v{k}][a{k}]"
        fc = parts + f"{ci}concat=n={len(chunk)}:v=1:a=1[v][a]"
        bf = os.path.join(tmp, f"b{bi//BATCH:03d}.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", fc,
             "-map", "[v]", "-map", "[a]", *venc,
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", bf])
        batches.append(bf)
        print(f"[cutv5]   Batch {bi//BATCH+1}/{(len(segs)+BATCH-1)//BATCH}", flush=True)

    if len(batches) == 1:
        os.replace(batches[0], out_mp4)
    else:
        lst = os.path.join(tmp, "l.txt")
        open(lst, "w").write("".join(f"file '{b}'\n" for b in batches))
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-c", "copy", out_mp4])
    print(f"[cutv5] FERTIG -> {out_mp4}", flush=True)

if __name__ == "__main__":
    main()
