#!/usr/bin/env python3
"""Shortform-Render-Hook fuers Cut-Cockpit (Fork): baut proxy.mp4 komplett neu
aus den Cockpit-Daten des Workdirs.

Quellen im Workdir:
  cockpit_overrides.json  timeline_clips [{id,in,out,gain}] = Schnitt (Quellzeiten)
                          broll [{start,end,file,src_in,speed?,...}] = Karten (Cut-Timeline)
  captions_sf.json        [{start,end,text,size?,words?:[{t0,t1,w}]}]
  sf_layout.json          Layout-Konstanten (Zonen, Karten-Geometrie, Hook-Assets,
                          Titel, Pill, Caption-Stil)

Kette: Schnitt -> Karten-Zwischenclips (Tempo-Fit via setpts) -> Caption-Layer
(PIL, Wort-Karaoke mit Pink-Box) -> Ein-Pass-Composite -> proxy.mp4 (atomar).
Usage: rebuild_sf.py <workdir> <src_video>
"""
import json
import os
import re
import subprocess
import sys

import numpy as np  # noqa: F401  (Haltung: gleiche venv wie Pipeline)
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rerender as _rr  # noqa: E402  (Look-Tabelle — EINE Implementierung)

WORKDIR = os.path.abspath(sys.argv[1])
SRC = os.path.abspath(sys.argv[2])
W, H, FPS = 1080, 1920, 30


def load(name, default):
    p = os.path.join(WORKDIR, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def run(cmd, label):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-3000:])
        raise SystemExit(f"[rebuild_sf] {label} fehlgeschlagen (rc={r.returncode})")
    return r


def probe_dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], f"probe {path}")
    return float(r.stdout.strip())


ovr = load("cockpit_overrides.json", {})
clips = ovr.get("timeline_clips") or []
slots = [b for b in (ovr.get("broll") or [])
         if b.get("file") and ((b.get("end", 0) > b.get("start", 0))
                               or b.get("clip_id") is not None
                               or b.get("clip_idx") is not None)]
caps = load("captions_sf.json", [])
lay = load("sf_layout.json", {})
if not clips:
    raise SystemExit("[rebuild_sf] keine timeline_clips in cockpit_overrides.json")

print(f"[rebuild_sf] {len(clips)} Schnitt-Clips, {len(slots)} B-Roll-Slots, {len(caps)} Text-Clips", flush=True)

tmpdir = os.path.join(WORKDIR, "rebuild_sf")
os.makedirs(tmpdir, exist_ok=True)

# --- 1) Schnitt ------------------------------------------------------------
seg_files = []
concat_list = os.path.join(tmpdir, "concat.txt")
with open(concat_list, "w") as lf:
    for i, c in enumerate(clips):
        a, b = float(c["in"]), float(c["out"])
        d = b - a
        if d <= 0.03:
            continue
        out = os.path.join(tmpdir, f"seg{i:02d}.mp4")
        gain = float(c.get("gain") or 0)
        af = (f"afade=t=in:st=0:d=0.03,afade=t=out:st={max(0, d-0.03):.4f}:d=0.03,"
              f"aformat=sample_rates=48000:channel_layouts=mono")
        if abs(gain) > 0.01:
            af = f"volume={gain}dB," + af
        # FARB-LOOK (01.09.): kompletter CapCut-Regler-Satz pro Clip —
        # identische Tabelle wie Longform-Render + Cockpit-Preview.
        vf = "scale=1080:1920,fps=30" + _rr.build_look_filter(c)
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.4f}", "-t", f"{d:.4f}", "-i", SRC,
             "-vf", vf, "-af", af,
             "-c:v", "libx264", "-crf", "17", "-preset", "fast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", out], f"Schnitt seg{i}")
        lf.write(f"file '{out}'\n")
        seg_files.append((out, d))
        print(f"[rebuild_sf] seg{i:02d}: {a:.2f}+{d:.2f}", flush=True)

cut_raw = os.path.join(tmpdir, "cut_raw.mp4")
cut = os.path.join(tmpdir, "cut.mp4")
run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat_list,
     "-c", "copy", cut_raw], "concat")
run(["ffmpeg", "-y", "-loglevel", "error", "-i", cut_raw, "-c:v", "copy",
     # -ar 48000 ist Pflicht: loudnorm gibt sonst mit ueberhoehter Rate aus und
     # der AAC-Encoder landet bei 96 kHz (groesser, und nicht jeder Player mag es).
     "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000",
     "-c:a", "aac", "-b:a", "192k", cut], "loudnorm")
dur_total = probe_dur(cut)
seg_bounds = []
t = 0.0
for _, d in seg_files:
    seg_bounds.append((t, t + d))
    t += d
print(f"[rebuild_sf] Schnitt: {dur_total:.2f}s", flush=True)

# --- clip-gebundene Slots (03.09.) -----------------------------------------
# Ein B-Roll gehoert zu genau einem Clip und fuellt ihn exakt aus. Die Grenzen
# kommen deshalb IMMER aus dem geschnittenen Clip (seg_bounds = gemessene
# Dauern nach dem Schnitt), nie aus gespeicherten Zeiten. Korrigiert Sebastian
# den Schnitt nach, passt speed:"fit" das Tempo automatisch an die neue Laenge
# an — ohne das B-Roll neu zu bauen.
_by_id = {c.get("id"): i for i, c in enumerate(clips) if c.get("id") is not None}
_gebunden = 0
for b in slots:
    ci = _by_id.get(b.get("clip_id"))
    if ci is None:
        ci = b.get("clip_idx")
    if ci is None or not (0 <= ci < len(seg_bounds)):
        continue
    b["start"], b["end"] = seg_bounds[ci]
    _gebunden += 1
if _gebunden:
    print(f"[rebuild_sf] {_gebunden} Slot(s) an ihren Clip gebunden", flush=True)

# --- Layout-Zonen ----------------------------------------------------------
body_t0 = seg_bounds[0][1] if len(seg_bounds) > 1 else 0.0
body_t1 = seg_bounds[-1][0] if len(seg_bounds) > 1 else dur_total
video_yoff = int(lay.get("video_yoff", 572))
card = lay.get("card", {"w": 1316, "h": 740, "x": -118, "y": 0})

# --- 2) Karten-Zwischenclips ----------------------------------------------
card_specs = []
for i, b in enumerate(slots):
    f = b["file"]
    if not os.path.exists(f):
        print(f"[rebuild_sf] WARNUNG: Slot-Datei fehlt, übersprungen: {f}", flush=True)
        continue
    t0, t1 = float(b["start"]), float(b["end"])
    t1 = min(t1, dur_total)
    d = t1 - t0
    if d <= 0.05:
        continue
    src_in = max(0.0, float(b.get("src_in") or 0))
    out = os.path.join(tmpdir, f"card{i:02d}.mp4")
    # FREIE GEOMETRIE (03.09.): importierte Medien tragen geo {x,y,w} in
    # normalisierten Design-Koordinaten (0..1, x/y = Mittelpunkt, w = Breite
    # relativ zu 1080). Ohne geo gilt die Karten-Standardlage aus sf_layout.
    geo = b.get("geo") or None
    if geo:
        gw = max(0.05, min(3.0, float(geo.get("w", 0.8))))
        ziel_w = int(round(gw * W / 2) * 2)
        ziel_h = -2                                   # Seitenverhaeltnis behalten
    else:
        ziel_w, ziel_h = card["w"], card["h"]
    if b.get("speed") == "fit":
        rest = max(0.1, probe_dur(f) - src_in)
        factor = d / rest
        vf = (f"scale={ziel_w}:{ziel_h},setpts=PTS*{factor:.6f},fps={FPS}")
        print(f"[rebuild_sf] card{i:02d}: Tempo-Fit x{1/factor:.2f} ({rest:.2f}s -> {d:.2f}s)", flush=True)
    else:
        vf = f"scale={ziel_w}:{ziel_h},tpad=stop_mode=clone:stop_duration=10,fps={FPS}"
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{src_in:.3f}", "-i", f,
         "-vf", vf, "-t", f"{d:.4f}", "-an",
         "-c:v", "libx264", "-crf", "17", "-preset", "fast", out], f"card{i}")
    card_specs.append((out, t0, t1, geo))

# --- 3) Caption-Layer (PIL, Wort-Karaoke + Pink-Box) -----------------------
capdir = os.path.join(tmpdir, "cap_png")
os.makedirs(capdir, exist_ok=True)
try:
    import caption_style as _cs
except ImportError:
    _cs = None
if _cs is None:
    print("[rebuild_sf] Caption-Modul nicht vorhanden — Text-Layer wird uebersprungen",
          flush=True)
    caps = []
CAP_STYLE = _cs.load_style(lay) if _cs else {}
font_path = CAP_STYLE["font"]
PINK = tuple(lay.get("caption", {}).get("pink", [212, 67, 106])) + (255,)
STROKE = int(lay.get("caption", {}).get("stroke", 7))
Y_BODY = int(lay.get("caption", {}).get("y_body", 787))
Y_FULL = int(lay.get("caption", {}).get("y_full", 1205))


def norm_words(text):
    return [w for w in re.split(r"\s+", (text or "").strip()) if w]


def cap_states(c):
    """Zustaende (words, aktiver Index, t0, t1) einer Text-Gruppe."""
    t0, t1 = float(c["start"]), float(c["end"])
    words = norm_words(c.get("text", ""))
    if not words:
        return []
    stored = c.get("words") or []
    if len(stored) == len(words) and all(
            (s.get("w") or "").strip() == words[k] for k, s in enumerate(stored)):
        st0, st1 = stored[0]["t0"], stored[-1]["t1"]
        if st0 >= t0 - 0.3 and st1 <= t1 + 0.3:
            # ABSOLUTE Wort-Zeiten (CapCut-Prinzip, captions_from_words.py):
            # exakt der Sprache folgen statt aufs Seitenfenster zu skalieren
            # (Skalieren = sichtbarer Karaoke-Drift, sobald das Fenster nicht
            # exakt sitzt). Identische Logik im Player (capWordTimes).
            times = [(max(t0, s["t0"]), min(t1, max(s["t0"] + 0.03, s["t1"])))
                     for s in stored]
        else:
            span = max(0.05, st1 - st0)
            times = [(t0 + (s["t0"] - st0) / span * (t1 - t0),
                      t0 + (s["t1"] - st0) / span * (t1 - t0)) for s in stored]
    else:
        lens = [max(1, len(w)) for w in words]
        tot = sum(lens)
        times, acc = [], t0
        for L in lens:
            nxt = acc + (t1 - t0) * L / tot
            times.append((acc, nxt))
            acc = nxt
    out = []
    for j in range(len(words)):
        s0 = times[j][0] if j else t0
        s1 = times[j + 1][0] - 0.01 if j + 1 < len(words) else t1
        if s1 <= s0:
            s1 = s0 + 0.04
        out.append((words, j, s0, s1))
    return out


_measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
entries = []
blank = os.path.join(capdir, "blank.png")
Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)
for gi, c in enumerate(sorted(caps, key=lambda x: float(x.get("start", 0)))):
    size = int(c.get("size") or 62)
    # Position: normalisierte x/y aus dem Player-Editing, sonst Zonen-Default
    y_mid = int(float(c["y"]) * H) if c.get("y") is not None else (
        Y_BODY if body_t0 <= float(c["start"]) < body_t1 else Y_FULL)
    x_mid = int(float(c["x"]) * W) if c.get("x") is not None else (W // 2)
    for (words, j, s0, s1) in cap_states(c):
        # EINE Zeichenfunktion fuer Render + Vorschau (caption_style.py)
        img = _cs.draw_caption(W, H, words, j, size, CAP_STYLE, x_mid, y_mid)
        p = os.path.join(capdir, f"g{gi:03d}_{j}.png")
        img.save(p)
        entries.append((p, s0, s1))

entries.sort(key=lambda e: e[1])
cap_concat = os.path.join(capdir, "concat.txt")
with open(cap_concat, "w") as f:
    cursor = 0.0
    lines = []
    for p, s0, s1 in entries:
        if s0 > cursor + 0.001:
            lines.append((blank, s0 - cursor))
        lines.append((p, max(0.04, s1 - s0)))
        cursor = max(cursor, s1)
    if cursor < dur_total:
        lines.append((blank, dur_total - cursor))
    for p, d in lines:
        f.write(f"file '{p}'\nduration {d:.4f}\n")
    f.write(f"file '{lines[-1][0]}'\n")
cap_layer = os.path.join(tmpdir, "cap_layer.mov")
run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", cap_concat,
     "-vsync", "vfr", "-c:v", "png", cap_layer], "cap_layer")
print(f"[rebuild_sf] Captions: {len(entries)} Zustaende", flush=True)

# --- 4) Statische Assets (Hook + Titel + Pill) -----------------------------
def title_png():
    t = lay.get("title") or {}
    if not t.get("lines"):
        return None
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    f1 = ImageFont.truetype(t.get("font", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                            int(t.get("size", 69)))
    y = int(t.get("y", 268))
    for ln in t["lines"]:
        tw = dr.textlength(ln, font=f1)
        dr.text(((W - tw) / 2, y), ln, font=f1, fill=(0, 0, 0, 255),
                stroke_width=8, stroke_fill=(255, 255, 255, 255))
        y += int(t.get("line_h", 93))
    p = os.path.join(tmpdir, "title.png")
    img.save(p)
    return p


def pill_png():
    t = lay.get("pill") or {}
    if not t.get("text"):
        return None
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    f1 = ImageFont.truetype(t.get("font", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                            int(t.get("size", 66)))
    txt = t["text"]
    tw = dr.textlength(txt, font=f1)
    asc, desc = f1.getmetrics()
    cx, cy = W / 2, int(t.get("y", 300))
    dr.rounded_rectangle([cx - tw/2 - 46, cy - (asc+desc)/2 - 26,
                          cx + tw/2 + 46, cy + (asc+desc)/2 + 26], radius=30,
                         fill=(255, 255, 255, 255))
    dr.text((cx - tw/2, cy - (asc+desc)/2), txt, font=f1, fill=(0, 0, 0, 255))
    p = os.path.join(tmpdir, "pill.png")
    img.save(p)
    return p


TITLE, PILL = title_png(), pill_png()
hook = lay.get("hook") or {}

# --- 5) Composite ----------------------------------------------------------
inputs = ["-i", cut]
for cpath, _, _, _ in card_specs:
    inputs += ["-i", cpath]
idx = len(card_specs) + 1
extra = []
def add_input(path):
    global idx
    inputs.extend(["-loop", "1", "-i", path])
    i = idx
    idx += 1
    return i

i_hf = add_input(hook["hf"]["file"]) if hook.get("hf", {}).get("file") and os.path.exists(hook.get("hf", {}).get("file", "")) else None
i_logo = add_input(hook["logo"]["file"]) if hook.get("logo", {}).get("file") and os.path.exists(hook.get("logo", {}).get("file", "")) else None
i_title = add_input(TITLE) if TITLE else None
i_pill = add_input(PILL) if PILL else None
inputs += ["-i", cap_layer]
i_cap = idx

fc = [f"color=white:s={W}x{H}:r={FPS}:d={dur_total:.3f}[bg]",
      "[0:v]setsar=1[v0]",
      f"[bg][v0]overlay=x=0:y='if(between(t,{body_t0:.3f},{body_t1:.3f}),{video_yoff},0)':shortest=1[base]"]
prev = "[base]"
for i, (cpath, t0, t1, geo) in enumerate(card_specs):
    fc.append(f"[{i+1}:v]setpts=PTS-STARTPTS+{t0:.3f}/TB[cd{i}]")
    if geo:
        # Mittelpunkt in Design-Koordinaten -> ffmpeg rechnet die Ecke selbst
        # (overlay_w/h kennt die tatsaechliche Groesse nach dem Skalieren).
        gx = max(-0.5, min(1.5, float(geo.get("x", 0.5))))
        gy = max(-0.5, min(1.5, float(geo.get("y", 0.5))))
        ox = f"{gx * W:.0f}-overlay_w/2"
        oy = f"{gy * H:.0f}-overlay_h/2"
    else:
        ox, oy = str(card.get("x", -118)), str(card.get("y", 0))
    fc.append(f"{prev}[cd{i}]overlay=x='{ox}':y='{oy}':"
              f"enable='between(t,{t0:.3f},{t1:.3f})':eof_action=pass[b{i}]")
    prev = f"[b{i}]"
if i_hf is not None:
    hf = hook["hf"]
    fc.append(f"[{i_hf}:v]scale={hf.get('scale_w', 919)}:-1[hf]")
    fc.append(f"{prev}[hf]overlay=x={hf.get('x', 80)}:y={hf.get('y', 1215)}:enable='lte(t,{body_t0:.3f})'[hh]")
    prev = "[hh]"
if i_logo is not None:
    lg = hook["logo"]
    fc.append(f"[{i_logo}:v]scale={lg.get('scale_w', 460)}:-1[lg]")
    fc.append(f"{prev}[lg]overlay=x={lg.get('x', 310)}:y={lg.get('y', 1738)}:enable='lte(t,{body_t0:.3f})'[ll]")
    prev = "[ll]"
if i_title is not None:
    fc.append(f"{prev}[{i_title}:v]overlay=0:0:enable='lte(t,{float((lay.get('title') or {}).get('show_s', 2.33)):.2f})'[tt]")
    prev = "[tt]"
if i_pill is not None:
    from_end = float((lay.get("pill") or {}).get("from_end_s", 2.34))
    fc.append(f"{prev}[{i_pill}:v]overlay=0:0:enable='gte(t,{max(0, dur_total - from_end):.3f})'[pp]")
    prev = "[pp]"
fc.append(f"{prev}[{i_cap}:v]overlay=0:0:eof_action=pass,format=yuv420p[vout]")

final_tmp = os.path.join(tmpdir, "proxy_new.mp4")
run(["ffmpeg", "-y", "-loglevel", "warning"] + inputs + [
    "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "0:a",
    "-c:v", "libx264", "-crf", "17", "-preset", "fast", "-pix_fmt", "yuv420p",
    "-c:a", "copy", "-t", f"{dur_total:.3f}", final_tmp], "Composite")

proxy = os.path.join(WORKDIR, "proxy.mp4")
os.replace(final_tmp, proxy)
print(f"[rebuild_sf] FERTIG: proxy.mp4 ersetzt ({probe_dur(proxy):.2f}s)", flush=True)
