#!/usr/bin/env python3
"""V5/V6 Cockpit-Render-Hook: Overrides -> Solver -> Nudges -> Render.

Kette:
1. decisions_effective = decisions.cut_word_ids + extra_cut_word_ids - uncut_word_ids
2. solver_v5.py neu ausfuehren -> segments_v5.json (frische, gemessene Kanten)
3. Nudges/Gains anwenden: Overrides speichern segIdx relativ zur UI-Basis
   (--nudge-base), Mapping erfolgt ueber first_id/last_id (stabil ueber Re-Solves)
4. Render (proxy 1080p / final source-res) mit per-Segment volume-Filter
5. Optional B-Roll-Overlay-Pass (Zeiten = Proxy-Timeline, Video-only, Ton bleibt)

Usage: rerender.py <workdir> <src_video> <words_json> <decisions_json> <out_mp4>
                   [--mode proxy|final] [--nudge-base segments.json]
"""
import sys, os, json, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = 25
FADE_S = 0.012
# Bildrate der Timeline. Das Quellmaterial ist 30 fps und der B-Roll-Sync-Pass
# rendert fest mit fps=30 — die Clipdauern werden darauf gerastet, damit Bild
# und Ton nicht auseinanderlaufen (siehe apply_jcut_edges).
FPS = 30.0

def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-3000:])
        raise SystemExit(f"Kommando fehlgeschlagen: {' '.join(cmd[:3])}…")
    return r

def main():
    args = sys.argv[1:]
    workdir, src, words_path, dec_path, out_mp4 = args[:5]
    mode = "proxy"; nudge_base = None
    if "--mode" in args: mode = args[args.index("--mode") + 1]
    if "--nudge-base" in args: nudge_base = args[args.index("--nudge-base") + 1]

    ovr_path = os.path.join(workdir, "cockpit_overrides.json")
    ovr = json.load(open(ovr_path)) if os.path.exists(ovr_path) else {}
    nudges = ovr.get("nudges", {}); gains = ovr.get("gains", {})
    broll = ovr.get("broll", []); extra = set(ovr.get("extra_cut_word_ids", []))
    uncut = set(ovr.get("uncut_word_ids", []))
    deleted = set(ovr.get("deleted_segments", []))   # first_ids (inkl. Split-Haelften "wN#sK")
    splits = sorted(ovr.get("splits", []))           # Source-Sekunden

    # 0. TIMELINE-MODUS (Cockpit v2.3): Wenn das Cockpit eine fertige Clip-Liste
    #    gespeichert hat, ist DIE die Quelle der Wahrheit — kein Solver, kein
    #    Split/Delete/Nudge-Mapping. {id, in, out, gain} pro Clip, in Reihenfolge.
    timeline_clips = ovr.get("timeline_clips") or []
    if timeline_clips:
        segs = []
        for tc in timeline_clips:
            if tc.get("out", 0) - tc.get("in", 0) < 0.04:
                continue
            segs.append({"in": round(float(tc["in"]), 4), "out": round(float(tc["out"]), 4),
                         "first_id": tc.get("id", f"tl{len(segs)}"),
                         "gain_db": float(tc.get("gain", 0) or 0),
                         "jcut": max(0.0, float(tc.get("jcut", 0) or 0)),
                         **{k: float(tc.get(k, 0) or 0) for k in LOOK_KEYS},
                         "flags": ["timeline"]})
        if segs:
            segs[0]["jcut"] = 0.0          # der erste Clip hat keine linke Naht
        apply_jcut_edges(segs, pauses=load_pause_map(os.path.dirname(os.path.abspath(ovr_path))))
        n_j = sum(1 for s in segs if s["jcut"] > 1e-3)
        # Fingerprint des Cockpit-Stands: beweist, WELCHER Stand gerendert
        # wird (Speicherzeit + Pruefsumme + Umfang). Gehoert in jede Uebergabe.
        import hashlib, time as _time
        _fp = hashlib.md5(open(ovr_path, "rb").read()).hexdigest()[:10]
        _mt = _time.strftime("%d.%m.%Y %H:%M:%S", _time.localtime(os.path.getmtime(ovr_path)))
        _soll = sum(s["v_out"] - s["v_in"] for s in segs)
        print(f"[rerender] Cockpit-Stand: gespeichert {_mt} (md5 {_fp}) — "
              f"{len(segs)} Clips, {len(broll)} B-Roll-Slots, Soll-Laenge {_soll:.3f}s", flush=True)
        print(f"[rerender] TIMELINE-Modus: {len(segs)} Clips direkt aus dem Cockpit"
              + (f", {n_j}× L-Cut" if n_j else ""), flush=True)
        json.dump({"segments": segs, "keep_s": round(sum(s['out']-s['in'] for s in segs), 1)},
                  open(os.path.join(workdir, "segments_effective.json"), "w"),
                  ensure_ascii=False, indent=1)
        render_segments(segs, src, out_mp4, mode, broll, workdir)
        return

    # 1. effektive Entscheidungen
    dec = json.load(open(dec_path))
    cut_ids = (set(dec.get("cut_word_ids", [])) | extra) - uncut
    dec_eff = {**dec, "cut_word_ids": sorted(cut_ids, key=lambda s: int(s[1:]))}
    eff_path = os.path.join(workdir, "decisions_effective.json")
    json.dump(dec_eff, open(eff_path, "w"), ensure_ascii=False, indent=0)
    print(f"[rerender] {len(cut_ids)} Cut-Woerter effektiv "
          f"(+{len(extra)} extra, -{len(uncut)} restauriert)", flush=True)

    # 2. Solver
    audio48 = os.path.join(workdir, "audio48k.wav")
    run([sys.executable, os.path.join(HERE, "solver_v5.py"),
         audio48, words_path, eff_path, workdir])
    seg_doc = json.load(open(os.path.join(workdir, "segments_v5.json")))
    segs = seg_doc["segments"]

    # 2b. Splits anwenden (Timeline-UI): Source-Zeit t im Segment → zwei Haelften.
    #     ID-Konvention (deterministisch, mit Cockpit-Frontend abgestimmt):
    #     linke Haelfte behaelt first_id, rechte bekommt "<first_id>#sK" (K = 1..n
    #     pro Ursprungs-Segment, Splits aufsteigend sortiert).
    if splits:
        applied_s = 0
        for t in splits:
            for k, s in enumerate(segs):
                if s["in"] + 0.05 < t < s["out"] - 0.05:
                    n_prev = sum(1 for x in segs if str(x["first_id"]).split("#")[0]
                                 == str(s["first_id"]).split("#")[0])
                    base_id = str(s["first_id"]).split("#")[0]
                    right = {**s, "in": round(t, 4),
                             "first_id": f"{base_id}#s{n_prev}",
                             "flags": list(s.get("flags", [])) + ["split"]}
                    left = {**s, "out": round(t, 4),
                            "flags": list(s.get("flags", [])) + ["split"]}
                    segs[k:k + 1] = [left, right]
                    applied_s += 1
                    break
            else:
                print(f"[rerender] Split bei {t:.2f}s verworfen (liegt in keinem Segment)", flush=True)
        print(f"[rerender] {applied_s} Splits angewendet", flush=True)

    # 2c. Geloeschte Segmente (Timeline-UI, per first_id)
    if deleted:
        before = len(segs)
        segs = [s for s in segs if str(s["first_id"]) not in deleted]
        print(f"[rerender] {before - len(segs)} Segmente geloescht "
              f"({len(deleted)} angefordert)", flush=True)

    # 3. Nudges + Gains von der UI-Basis auf den neuen Solve mappen:
    #    in-Kante gehoert zum ERSTEN Wort (first_id), out-Kante zum LETZTEN
    #    (last_id) — nach Re-Solve koennen Segmente anders geschnitten sein
    by_first = {s["first_id"]: s for s in segs}
    by_last = {s["last_id"]: s for s in segs}
    applied_n, applied_g = 0, 0
    if nudge_base and os.path.exists(nudge_base) and (nudges or gains):
        base_segs = json.load(open(nudge_base))["segments"]
        for key, delta in nudges.items():
            idx_s, edge = key.split(":")
            i = int(idx_s)
            if i >= len(base_segs) or edge not in ("in", "out"):
                print(f"[rerender] Nudge {key} verworfen (ungültig)", flush=True); continue
            anchor = base_segs[i]["first_id"] if edge == "in" else base_segs[i]["last_id"]
            tgt = (by_first if edge == "in" else by_last).get(anchor)
            if tgt is None:
                print(f"[rerender] Nudge {key} verworfen (Segment nach Re-Solve weg)", flush=True)
                continue
            tgt[edge] = round(tgt[edge] + float(delta), 4)
            applied_n += 1
        for idx_s, db in gains.items():
            i = int(idx_s)
            if i >= len(base_segs): continue
            tgt = by_first.get(base_segs[i]["first_id"])
            if tgt is None:
                print(f"[rerender] Gain für Basis-Segment {i} verworfen (Segment weg)", flush=True)
                continue
            tgt["gain_db"] = float(db); applied_g += 1
        # Clamping: Nudges duerfen Segmente weder invertieren noch in Nachbarn schieben
        for k, s in enumerate(segs):
            nxt_in = segs[k + 1]["in"] if k + 1 < len(segs) else float("inf")
            prv_out = segs[k - 1]["out"] if k > 0 else 0.0
            s["in"] = max(s["in"], prv_out + 0.005)
            s["out"] = max(s["in"] + 0.05, min(s["out"], nxt_in - 0.005))
    print(f"[rerender] {applied_n} Nudges, {applied_g} Gains angewendet", flush=True)
    seg_doc["segments"] = segs
    json.dump(seg_doc, open(os.path.join(workdir, "segments_effective.json"), "w"),
              ensure_ascii=False, indent=1)
    render_segments(segs, src, out_mp4, mode, broll, workdir)


LOOK_KEYS = ("exp", "bri", "con", "sat", "tmp", "tnt", "shp", "hig", "sha")


def build_look_filter(c):
    """CapCut-Farb-Look pro Clip -> ffmpeg-Filterkette (mit fuehrendem Komma).

    CapCut-Skala -50..+50 (Schaerfe 0..100). Die exakten CapCut-Formeln sind
    nicht oeffentlich — exp und sat sind deshalb GEMESSEN (03.09.2026), nicht
    geraten: der Rohaufnahme IMG_0910 gegen seinen CapCut-Export des
    gleichen Videos (Tag 210), Frames geometrisch registriert (Talking Head
    im Export 1.22x skaliert, ~195px versetzt), Delta in CIELAB auf fester
    Pixelmaske. Sein Standard sat +5 / exp -5 wirkt dort dL*=-6.09 bei
    dC*=+1.6%. Diese Kalibrierung trifft das auf dL*=-6.09 / dC*=+1.7%.
    Messkript + Frames: siehe Commit-Nachricht.
    Die uebrigen Achsen bleiben auf der Schaetzung von 01.09.:
      exp  Belichtung   EV = v/25           (exposure-Filter)
      bri  Helligkeit   additiv v/250       (eq=brightness)
      con  Kontrast     Faktor 1+v/100      (eq=contrast)
      sat  Saettigung   Faktor 1+v/36       (eq=saturation)
      tmp  Temperatur   6500K − v*35        (colortemperature; +v = waermer)
      tnt  Tint         gm = −v/150         (colorbalance; +v = magenta)
      hig  Highlights   Kurvenpunkt 0.75 +/- v/500 (curves)
      sha  Schatten     Kurvenpunkt 0.25 +/- v/500 (curves)
      shp  Schaerfe     unsharp amount v/66 (max 1.5)
    IDENTISCHE Abbildung in der Cockpit-Live-Preview (clipLookCss/feColorMatrix).
    """
    def g(k):
        return float(c.get(k, 0) or 0)
    f = ""
    if abs(g("exp")) > 0.01:
        f += f",exposure=exposure={g('exp')/25.0:.3f}"
    eq = []
    if abs(g("bri")) > 0.01:
        eq.append(f"brightness={g('bri')/250.0:.4f}")
    if abs(g("con")) > 0.01:
        eq.append(f"contrast={1 + g('con')/100.0:.3f}")
    if abs(g("sat")) > 0.01:
        eq.append(f"saturation={1 + g('sat')/36.0:.3f}")
    if eq:
        f += ",eq=" + ":".join(eq)
    if abs(g("tmp")) > 0.01:
        f += f",colortemperature=temperature={6500 - g('tmp')*35:.0f}"
    if abs(g("tnt")) > 0.01:
        f += f",colorbalance=gm={-g('tnt')/150.0:.4f}"
    if abs(g("hig")) > 0.01 or abs(g("sha")) > 0.01:
        y1 = min(0.45, max(0.05, 0.25 + g("sha")/500.0))
        y2 = min(0.95, max(0.55, 0.75 + g("hig")/500.0))
        f += f",curves=master='0/0 0.25/{y1:.3f} 0.75/{y2:.3f} 1/1'"
    if g("shp") > 0.01:
        f += f",unsharp=5:5:{min(2.0, g('shp')/66.0):.3f}"
    return f


def load_pause_map(workdir):
    """Waveform-gemessene Sprechpausen (pause_map.py) auf der FILE-Achse.
    None wenn keine Karte existiert — dann kann der Silence-Clamp nicht
    greifen und apply_jcut_edges warnt bei gesetzten J-Cuts."""
    p = os.path.join(workdir, "pause_map.json")
    if not os.path.exists(p):
        return None
    try:
        doc = json.load(open(p))
        off = 0.0
        if doc.get("aligned") is False and isinstance(doc.get("audio_start"), (int, float)):
            off = float(doc["audio_start"])
        return sorted([a + off, b + off] for a, b in doc.get("pauses", []))
    except Exception as e:  # noqa: BLE001
        print(f"[rerender] ⚠ pause_map.json unlesbar ({e!r}) — Silence-Clamp inaktiv", flush=True)
        return None


def _tail_silence_at(pauses, t):
    """Wieviel gemessene Stille liegt direkt vor Quellzeit t? (0 = Sprache)"""
    import bisect
    if not pauses:
        return 0.0
    i = bisect.bisect_right([p[0] for p in pauses], t) - 1
    if i < 0:
        return 0.0
    s, e = pauses[i]
    if t > e + 0.03:
        return 0.0
    return max(0.0, min(t, e) - s)


def _head_silence_at(pauses, t):
    """Wieviel gemessene Stille liegt direkt nach Quellzeit t?"""
    import bisect
    if not pauses:
        return 0.0
    i = bisect.bisect_right([p[0] for p in pauses], t) - 1
    if i >= 0:
        s, e = pauses[i]
        if t <= e + 0.001:
            return max(0.0, e - max(t, s))
    if i + 1 < len(pauses) and pauses[i + 1][0] - t <= 0.03:
        return pauses[i + 1][1] - pauses[i + 1][0]
    return 0.0


def apply_jcut_edges(segs, pauses=None):
    """Leitet Ton-Positionen und Bildkanten aus jcut ab (J-Cut / Split-Edit).

    jcut(i) = Sekunden, um die der TON von Segment i FRUEHER beginnt als sein
    Bild. Man hoert den naechsten Clip schon, waehrend man noch den vorherigen
    sieht.

        a_start(i) = a_end(i-1) - jcut(i)     Ton, Laenge bleibt dur(i)
        v_start(i) = a_end(i-1)               Bild, lueckenlos am Vorgaenger
        v_in(i)    = in(i) + jcut(i)          Bild verliert seinen ANFANG
        v_out(i)   = out(i)

    Bild und Ton bleiben dadurch im ganzen Segment synchron. Es wird KEIN
    geschnittenes Material zurueckgeholt und nichts verlaengert — das Bild wird
    nur vorne kuerzer.

    Folge: die Toene benachbarter Segmente ueberlappen sich um jcut(i), und die
    Gesamtlaenge sinkt um Σ jcut. Deshalb kann die Tonkette NICHT mehr per
    concat gebaut werden (siehe render_base_split).

    Clampt defensiv (das Cockpit clampt schon, der Render verlaesst sich nicht
    darauf).
    """
    MIN_VDUR = 0.05
    SIL_MARGIN = 0.02      # Abstand zur gemessenen Sprach-Kante (wie Cockpit)
    MIN_USEFUL = 0.04      # Mini-Vorlaeufe < 40ms bringen nichts
    n = len(segs)
    n_sil_clamped = 0
    for i, s in enumerate(segs):
        s["_dur"] = s["out"] - s["in"]
    for i, s in enumerate(segs):
        j = 0.0 if i == 0 else max(0.0, float(s.get("jcut", 0) or 0))
        if i > 0:
            j = min(j, max(0.0, s["_dur"] - MIN_VDUR))                       # eigenes Bild
            j = min(j, max(0.0, segs[i-1]["_dur"] - float(segs[i-1].get("jcut", 0) or 0) - 0.02))
            nxt = float(segs[i+1].get("jcut", 0) or 0) if i + 1 < n else 0.0
            j = min(j, max(0.0, s["_dur"] - nxt - 0.02))                     # Nicht-Nachbarn
            # ECHO-CLAMP (01.09.): im Ueberlapp-Fenster spielen A-Schwanz und
            # B-Kopf GLEICHZEITIG. Doppel-Sprache ist nur unmoeglich, wenn
            # j <= tailSilence(A.out) + headSilence(B.in) — identische Formel
            # im Cockpit (jcutEffOf). An normalen Naehten deckt Bs Anlauf-
            # Stille den A-Schwanz (L-Cut bleibt voll), an Split-Naehten geht
            # j gegen 0 (dort entstand das Echo vom 30.08.). Wirkt nur auf
            # den Render, die Overrides bleiben unangetastet.
            if pauses is not None and j > 1e-4:
                room = max(0.0, _tail_silence_at(pauses, segs[i-1]["out"])
                           + _head_silence_at(pauses, s["in"]) - SIL_MARGIN)
                if j - room > 1e-4:
                    n_sil_clamped += 1
                    j = room
            if 0 < j < MIN_USEFUL:
                j = 0.0
        s["jcut"] = round(j, 4)
    if n_sil_clamped:
        print(f"[rerender] Silence-Clamp: {n_sil_clamped} J-Cut(s) auf die "
              f"gemessene Naht-Stille geklemmt", flush=True)
    if pauses is None and any(float(s.get("jcut", 0) or 0) > 1e-3 for s in segs[1:]):
        print("[rerender] ⚠ J-Cuts gesetzt, aber KEINE pause_map.json — Clamp "
              "inaktiv, Vorlauf kann in Sprache laufen. Bauen: "
              "python scripts/pause_map.py <workdir>", flush=True)
    # FRAME-QUANTISIERUNG (01.08.2026) — sonst driftet das Bild gegen den Ton.
    #
    # ffmpeg schneidet Video zwangslaeufig auf Frame-Grenzen, Audio aber
    # sample-genau. Bei 213 Clips summieren sich diese Rundungen: gemessen
    # 304 ms Vorlauf des Bildes am Videoende — deutlich sichtbarer
    # Lippen-Versatz, und er wird gegen Ende immer groesser.
    #
    # Fix: die BILDdauer jedes Clips auf ein exaktes Frame-Vielfaches legen und
    # die gesamte Zeitachse (auch die Tonpositionen) daraus ableiten. Dann
    # rundet ffmpeg nichts mehr nach, und Σ Bilddauern == Tonende exakt.
    # Die Tondauer verschiebt sich dadurch um maximal eine halbe Frame-Laenge
    # pro Clip (< 17 ms) — unhoerbar, aber es haelt Bild und Ton zusammen.
    v_start = 0.0
    for i, s in enumerate(segs):
        j = float(s["jcut"])
        vdur = s["_dur"] - j                                  # Bilddauer, exakt
        vdur_q = max(1, round(vdur * FPS)) / FPS               # auf Frames rasten
        # 6 Nachkommastellen: eine Framelaenge ist 0.0333... s und laesst sich
        # auf 4 Stellen nicht sauber darstellen — der Restfehler summierte sich
        # sonst wieder ueber 213 Clips.
        s["v_start"] = round(v_start, 6)
        s["v_in"] = round(s["in"] + j, 6)
        s["v_out"] = round(s["in"] + j + vdur_q, 6)            # Frame-Vielfaches
        s["a_start"] = round(v_start - j, 6)
        # UEBERLAPP-SEMANTIK ZURUECK (01.09.): Der Ton jedes Segments spielt
        # seine VOLLE Laenge — die Toene benachbarter Segmente ueberlappen um
        # jcut (zwei alternierende Ketten + amix, wie urspruenglich gebaut).
        #
        # Die ROLL-Kuerzung vom 30.08. (a_dur = vdur_q + j − j_next) sollte
        # den Doppelton-Befund fixen, hat aber bei jcut > echte Naht-Stille
        # das LETZTE WORT jedes Segments mitten im Phonem gekappt (yt1b: 79%
        # der 95 Naehte, Median-Stille 0,12s vs. jcut 0,25s) — Sebastians
        # "Stotterer an jedem L-Cut". Der Doppelton selbst entstand aus der
        # gleichen Fehlannahme (fester jcut ohne Stille-Pruefung). Beides
        # fixt jetzt der SILENCE-CLAMP oben: jcut kann nie groesser sein als
        # die per Waveform gemessene Stille am Vorgaenger-Ende -> es
        # ueberlappt nur noch Stille mit Sprache. Kein Echo, kein Kappen.
        s["a_dur"] = round(vdur_q + j, 6)      # Ton endet mit dem eigenen Bild
        v_start += vdur_q
    for s in segs:
        s.pop("_dur", None)
    return segs


def probe_durations(path):
    """(video_dauer, audio_dauer) in Sekunden — fuer den A/V-Assert."""
    out = {}
    for st in ("v", "a"):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", st,
                            "-show_entries", "stream=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", path],
                           capture_output=True, text=True)
        try:
            out[st] = float((r.stdout or "").strip().splitlines()[0])
        except Exception:
            out[st] = None
    return out.get("v"), out.get("a")


def assert_av_sync(path, tol_ms=100.0):
    """Nach einem Split-Edit-Render ist die A/V-Kopplung nicht mehr strukturell
    durch concat=v=1:a=1 garantiert — also wird sie hier gemessen."""
    v, a = probe_durations(path)
    if v is None or a is None:
        print(f"[rerender] A/V-Check: Dauer nicht lesbar (v={v}, a={a})", flush=True)
        return
    d = abs(v - a) * 1000.0
    print(f"[rerender] A/V-Check: Video {v:.3f}s, Audio {a:.3f}s, Differenz {d:.1f} ms", flush=True)
    if d > tol_ms:
        raise SystemExit(f"A/V-Drift {d:.1f} ms > {tol_ms:.0f} ms — Render verworfen")


def render_base_split(segs, src, mode, vf_extra, venc, tmp):
    """Render mit J-Cut: Bild- und Tonkette getrennt bauen, am Ende muxen.

    Bild: [v_in,v_out] je Segment, lueckenlos, in Batches, concat=v=1:a=0.
    Ton:  die Stuecke UEBERLAPPEN sich um jcut — concat kann das nicht.
          Weil sich immer nur DIREKTE Nachbarn ueberlappen (dafuer sorgt das
          Clamping), genuegen zwei alternierende Ketten: gerade Segmente in
          Kette A, ungerade in Kette B, dazwischen Stille aus anullsrc. Beide
          Ketten sind in sich ueberlappungsfrei und damit concat-faehig; am
          Ende mischt ein einziges amix=inputs=2 die beiden zusammen.
          Das ist deutlich sparsamer als amix ueber alle 224 Stuecke.

    Der Crossfade in der Ueberlappung entsteht aus den afade-Rampen: das
    auslaufende Stueck blendet ueber jcut aus, das einsetzende ueber jcut ein.
    """
    abr = "320k" if mode == "max" else "192k"
    vsegs = [s for s in segs if s["v_out"] > s["v_in"]]
    asegs = [s for s in segs if s["out"] > s["in"]]
    n_j = sum(1 for s in segs if float(s.get("jcut", 0) or 0) > 1e-3)
    sum_j = sum(float(s.get("jcut", 0) or 0) for s in segs)
    total_v = sum(s["v_out"] - s["v_in"] for s in vsegs)
    total_a = max(s["a_start"] + float(s.get("a_dur", s["out"] - s["in"])) for s in asegs)
    print(f"[rerender] J-CUT: {len(vsegs)} Bildclips / {len(asegs)} Tonclips, "
          f"{n_j}× J-Cut ({sum_j:.1f}s Vorlauf gesamt), mode={mode}", flush=True)
    print(f"[rerender]   Soll-Laenge Bild {total_v:.3f}s / Ton {total_a:.3f}s "
          f"(Differenz {abs(total_v-total_a)*1000:.1f} ms)", flush=True)
    if abs(total_v - total_a) > 0.05:
        raise SystemExit(f"J-Cut-Mathematik verletzt: Bild {total_v:.3f}s != Ton {total_a:.3f}s")

    # ---- Bildkette (Batches, ohne Audio) ----
    vparts = []
    for bi in range(0, len(vsegs), BATCH):
        chunk = vsegs[bi:bi + BATCH]; parts = ""; ci = ""
        for k, s in enumerate(chunk):
            # fps={FPS} ZWINGEND: Kameraquellen laufen selten exakt mit der
            # deklarierten Rate (yt1b: 29.9885 statt 30). Ohne diesen Filter
            # erbt der Schnitt die krumme Rate, waehrend der Ton in exakten
            # Sekunden geschnitten wird — das Bild eilt dann linear vor
            # (gemessen: 617 ms nach 26 Minuten, am Ende deutlich sichtbar).
            # Der Filter dupliziert/verwirft dafuer etwa alle 2600 Frames
            # einen einzigen — unsichtbar.
            # Exakte Frame-Zahl erzwingen. fps= allein reicht NICHT: bei einer
            # krummen Quellrate (29.9885) liefert der Filter mal einen Frame
            # zu wenig — ueber 213 Clips fehlten so 10 Frames = 333 ms Drift.
            # Deshalb: etwas Reserve schneiden, auf 30 fps rastern, dann per
            # select exakt die ersten N Frames behalten.
            nfr = max(1, round((s['v_out'] - s['v_in']) * FPS))
            # tpad klont den letzten Frame, falls die Reserve nicht reicht
            # (z.B. am Dateiende) — sonst liefert select weniger als nfr und
            # der Clip ist einen Frame zu kurz. select begrenzt danach exakt.
            # FARB-LOOK (01.09.): kompletter CapCut-Regler-Satz pro Clip
            # (build_look_filter — eine Tabelle fuer beide Renderer + Preview)
            look = build_look_filter(s)
            parts += (f"[0:v]trim={s['v_in']:.4f}:{s['v_out'] + 0.2:.4f},"
                      f"setpts=PTS-STARTPTS,fps={FPS:g},"
                      f"tpad=stop_mode=clone:stop_duration=0.5,"
                      + "select='lt(n\\," + str(nfr) + ")',setpts=N/" + f"{FPS:g}" + "/TB"
                      f"{look}{vf_extra}[v{k}];")
            ci += f"[v{k}]"
        # setpts NACH dem concat: concat setzt die PTS des Folgeclips ans Ende
        # des vorherigen — durch Zeitbasis-Rundung landen dabei einzelne Frames
        # minimal neben dem 30fps-Raster. Ein Encoder mit -vsync cfr rastert neu
        # und verwirft dann den Frame, der auf einem schon belegten Platz landet
        # (gemessen: Filterkette liefert 5272, Datei hatte 5271). Dieses setpts
        # legt die PTS auf ein exaktes Raster, -fps_mode passthrough laesst sie
        # danach unangetastet — der Encoder darf nicht mehr selbst rastern.
        fc = parts + f"{ci}concat=n={len(chunk)}:v=1:a=0,setpts=N/{FPS:g}/TB[v]"
        bf = os.path.join(tmp, f"v{bi//BATCH:03d}.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", fc,
             "-map", "[v]", "-an", *venc,
             "-fps_mode", "passthrough", "-video_track_timescale", str(int(FPS * 1000)),
             bf])   # kein Re-Rastern: jeder gelieferte Frame landet in der Datei
        vparts.append((bf, sum(max(1, round((s['v_out'] - s['v_in']) * FPS)) for s in chunk)))
        print(f"[rerender]   Bild-Batch {bi//BATCH+1}/{(len(vsegs)+BATCH-1)//BATCH}", flush=True)
    vid = os.path.join(tmp, "video.mp4")
    if len(vparts) == 1:
        os.replace(vparts[0][0], vid)
    else:
        # KRITISCH: explizite duration-Zeilen (Framezahl/FPS). Der mp4-Muxer
        # meldet die Container-Dauer jedes Batches um genau EINE Bilddauer zu
        # kurz (das letzte Sample hat keinen Folge-PTS). Der concat-Demuxer
        # schiebt jede Folgedatei um die GEMELDETE Dauer nach hinten — pro
        # Naht rutschte so alles 33 ms nach vorn (yt1b: 9 Batches = 267 ms
        # Bild-vor-Ton am Ende, und -shortest schnitt den Ton entsprechend
        # frueher ab -> fehlendes Schlusswort). Mit expliziten Dauern liegt
        # jedes Bild exakt auf dem Raster — verifiziert per PTS-Scan ueber
        # alle 47128 Pakete (max. Abweichung 0.00 ms).
        lst = os.path.join(tmp, "lv.txt")
        open(lst, "w").write("ffconcat version 1.0\n" + "".join(
            f"file '{b}'\nduration {n / FPS:.6f}\n" for b, n in vparts))
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-c", "copy", vid])

    # ---- Tonkette: zwei alternierende Ketten + amix ----
    AF = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    jc = [float(s.get("jcut", 0) or 0) for s in asegs]
    parts, chains = "", []
    for par in (0, 1):
        idx = [k for k in range(len(asegs)) if k % 2 == par]
        if not idx:
            continue
        seq, cursor = "", 0.0
        for n, k in enumerate(idx):
            s = asegs[k]
            # a_dur statt (out-in): die Tondauer folgt der frame-gerasteten
            # Bilddauer, sonst driften beide wieder auseinander.
            st = s["in"]
            dur = float(s.get("a_dur", s["out"] - s["in"]))
            e = st + dur
            pos = s["a_start"]
            gap = pos - cursor
            if gap > 0.0005:                       # Stille bis zum Stueckanfang
                parts += f"[1:a]atrim=0:{gap:.4f},asetpts=PTS-STARTPTS,{AF}[p{par}s{n}];"
                seq += f"[p{par}s{n}]"
            # NUR Declick-Fades (12ms), KEINE Kreuzblende ueber die Ueberlappung.
            # Beide Toene sollen in voller Lautstaerke laufen: der vorherige
            # Clip wird nicht leiser, der neue kommt darunter dazu. Eine
            # Kreuzblende wuerde den vorherigen Ton faktisch kuerzen.
            fin = min(FADE_S, dur / 2.0)
            fout = min(FADE_S, dur / 2.0)
            g = s.get("gain_db", 0.0)
            vol = f",volume={g:.1f}dB" if abs(g) > 0.01 else ""
            parts += (f"[0:a]atrim={st:.4f}:{e:.4f},asetpts=PTS-STARTPTS,"
                      f"afade=t=in:st=0:d={fin:.4f},"
                      f"afade=t=out:st={dur-fout:.4f}:d={fout:.4f}{vol},{AF}[p{par}a{n}];")
            seq += f"[p{par}a{n}]"
            cursor = pos + dur
        nseg = seq.count("[")
        parts += f"{seq}concat=n={nseg}:v=0:a=1[chain{par}];"
        chains.append(f"[chain{par}]")
    if len(chains) == 2:
        fc = parts + f"{chains[0]}{chains[1]}amix=inputs=2:normalize=0:dropout_transition=0[a]"
    else:
        fc = parts + f"{chains[0]}anull[a]"
    aud = os.path.join(tmp, "audio.m4a")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-filter_complex", fc, "-map", "[a]",
         "-c:a", "aac", "-b:a", abr, "-ar", "48000", aud])
    print(f"[rerender]   Tonkette fertig ({len(asegs)} Clips in 2 Ketten, "
          f"{n_j} Ueberlappungen, beide Toene in voller Lautstaerke)", flush=True)

    # ---- Muxen ----
    # KEIN -shortest: beide Ketten sind per Konstruktion gleich lang. -shortest
    # hatte den Ton an der (vorher zu kurz gemeldeten) Video-Dauer abgeschnitten
    # — so fehlte das Schlusswort ("... im naechsten Video.").
    base = os.path.join(tmp, "base.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", vid, "-i", aud,
         "-map", "0:v", "-map", "1:a", "-c", "copy", base])
    assert_av_sync(base)
    return base


def render_segments(segs, src, out_mp4, mode, broll, workdir):
    """Schritt 4+5: Segmente rendern (per-Segment-Gain) + optionaler B-Roll-Pass."""
    # Die Tonketten-Mechanik entscheidet ueber den Encoder, NICHT die Frage, ob
    # echte J-Cuts vorliegen: seit dem 15.08.-Fix laeuft der Split-Pfad fuer
    # JEDE Timeline (jcut=0 ist dort der Grenzfall), siehe has_jcut unten. Die
    # Bedingung muss deshalb dieselbe sein — sonst bekommt jedes jcut-freie
    # Projekt J-Cut-Tonketten MIT VideoToolbox, also genau die unten
    # beschriebene verbotene Paarung. (mod4v8, 28.08.: gemessen 140 ms
    # konstanter Tonversatz bei PTS-Raster 0,0003 ms und A/V-Dauerdiff 0,0 ms —
    # weder Raster noch Container-Vergleich faengt das, nur Ton-gegen-Quelle.)
    tonketten = (any(float(s.get("jcut", 0) or 0) > 1e-3 for s in segs)
                 or bool(segs and "v_start" in segs[0]))
    if mode == "proxy":
        vf_extra = ",scale=-2:1080"
        if tonketten:
            # SPLIT-PFAD (J-Cut-Tonketten): bewiesene Software-Kombination
            # (mod4v1: 0,2 ms PASS). Die Paarung J-Cut-Tonketten +
            # h264_videotoolbox erzeugte einen konstanten 133-ms-A/V-Versatz
            # (+4 Frames) ueber die ganze Datei (mod4v2, 15.08., verify FAIL)
            # — bis zur Klaerung bleibt hier libx264.
            venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p"]
        else:
            # HARDWARE-ENCODER (15.08.): 3-5x schneller; nur noch fuer den
            # alten concat-Normal-Pfad ohne Tonketten.
            venc = ["-c:v", "h264_videotoolbox", "-b:v", "10M",
                    "-profile:v", "high", "-pix_fmt", "yuv420p"]
    elif mode == "max":
        # Master-Qualitaet fuer den Upload. Dieser Schritt ist nur ein
        # ZWISCHENprodukt (der Sync-Pass encodiert danach nochmal), deshalb hier
        # praktisch verlustfrei: CRF 13 gibt der zweiten Generation genug
        # Reserve. Der Hardware-Encoder (hevc_videotoolbox) waere zwar
        # schneller, verliert bei gleicher Datenrate aber sichtbar mehr Details
        # — und dieser Verlust waere danach nicht mehr aufzuholen.
        vf_extra = ""
        venc = ["-c:v", "libx264", "-preset", "fast", "-crf", "13", "-pix_fmt", "yuv420p"]
    else:
        vf_extra = ""
        venc = ["-c:v", "hevc_videotoolbox", "-q:v", "55", "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]
    abr = "320k" if mode == "max" else "192k"

    # --- Renderpfad-Wahl (15.08.): IMMER der Split-Pfad, wenn die frame-
    # gerasterten Kanten vorliegen (Timeline-Modus, apply_jcut_edges gelaufen).
    # Er ist der bewiesene exakte Weg: mod4v1 misst darueber 0,2 ms Lippensync
    # ueber 41 Clips (verify_render PASS). Der alte concat-Normal-Pfad behielt
    # selbst nach dem Raster-Fix einen konstanten ~21-ms-Ton-Offset (AAC-
    # Priming an den Batch-Naehten) und vereinzelte 2-Frame-Rasterstellen
    # (mod4v4 FAIL). jcut=0 ist im Split-Pfad der saubere Grenzfall.
    has_jcut = (any(float(s.get("jcut", 0) or 0) > 1e-3 for s in segs)
                or bool(segs and "v_start" in segs[0]))
    tmp = tempfile.mkdtemp(prefix="rer_")

    if has_jcut:
        base = render_base_split(segs, src, mode, vf_extra, venc, tmp)
    else:
        # FRAME-RASTER (15.08.): apply_jcut_edges legt die Bilddauern auf
        # exakte 30fps-Vielfache (v_in/v_out) — der Normal-Pfad nutzte aber
        # die ROHEN in/out. Der halbe Frame Rundung pro Segment akkumulierte
        # ueber die concat-Kette (mod4v4: 110 Clips -> +680 ms Drift am Ende,
        # verify_render FAIL; mod4v1 ueber den J-Cut-Pfad: 0,0 ms PASS).
        # Fallback auf in/out fuer den Solver-Pfad ohne v_in/v_out.
        pairs = [(s.get("v_in", s["in"]), s.get("v_out", s["out"]), s.get("gain_db", 0.0))
                 for s in segs if s.get("v_out", s["out"]) > s.get("v_in", s["in"])]
        print(f"[rerender] Render {len(pairs)} Segmente, mode={mode}", flush=True)
        batches = []
        for bi in range(0, len(pairs), BATCH):
            chunk = pairs[bi:bi + BATCH]; parts = ""; ci = ""
            for k, (s, e, g) in enumerate(chunk):
                fo = max(0.0, (e - s) - FADE_S)
                vol = f",volume={g:.1f}dB" if abs(g) > 0.01 else ""
                parts += (f"[0:v]trim={s:.4f}:{e:.4f},setpts=PTS-STARTPTS,fps={FPS:g}{vf_extra}[v{k}];"
                          f"[0:a]atrim={s:.4f}:{e:.4f},asetpts=PTS-STARTPTS,"
                          f"afade=t=in:st=0:d={FADE_S},afade=t=out:st={fo:.4f}:d={FADE_S}{vol}[a{k}];")
                ci += f"[v{k}][a{k}]"
            fc = parts + f"{ci}concat=n={len(chunk)}:v=1:a=1[v][a]"
            bf = os.path.join(tmp, f"b{bi//BATCH:03d}.mp4")
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", fc,
                 "-map", "[v]", "-map", "[a]", *venc, "-c:a", "aac", "-b:a", abr,
                 "-ar", "48000", "-r", f"{FPS:g}", "-vsync", "cfr", bf])
            batches.append(bf)
            print(f"[rerender]   Batch {bi//BATCH+1}/{(len(pairs)+BATCH-1)//BATCH}", flush=True)
        base = os.path.join(tmp, "base.mp4")
        if len(batches) == 1:
            os.replace(batches[0], base)
        else:
            lst = os.path.join(tmp, "l.txt")
            open(lst, "w").write("".join(f"file '{b}'\n" for b in batches))
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", lst, "-c", "copy", base])

    # 5. B-Roll-Overlay-Pass (Zeiten beziehen sich auf die NEUE Proxy-Timeline)
    #    ACHTUNG: Wenn ein Sync-Pass folgt (broll_sync.json existiert), rendert
    #    DER die Slots — hier einbrennen wuerde die Facecam-Kreis-Quelle
    #    zerstoeren (Kreis zeigte dann die Grafik statt der Gesicht).
    valid_broll = [b for b in broll if b.get("file") and os.path.exists(b["file"])
                   and b.get("end", 0) > b.get("start", 0)]
    if valid_broll and os.path.exists(os.path.join(workdir, "broll_sync.json")):
        print(f"[rerender] {len(valid_broll)} Slots -> rendert der Sync-Pass "
              f"(Facecam-Quelle bleibt sauber)", flush=True)
        valid_broll = []
    if valid_broll:
        print(f"[rerender] B-Roll-Pass: {len(valid_broll)} Slots", flush=True)
        probe = run(["ffprobe", "-v", "error", "-select_streams", "v",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0", base])
        W, H = probe.stdout.strip().split("\n")[0].split(",")[:2]
        inputs = ["-i", base]
        fc, prev = "", "0:v"
        for i, b in enumerate(valid_broll):
            inputs += ["-i", b["file"]]
            # src_in: Datei-Offset (frei geschnittene Grafik-B-Rolls zeigen den
            # richtigen Ausschnitt; Legacy-Slots ohne src_in starten bei 0)
            si = float(b.get("src_in", 0.0))
            dur = b["end"] - b["start"]
            fc += (f"[{i+1}:v]trim={si:.3f}:{si + dur + 0.5:.3f},setpts=PTS-STARTPTS,"
                   f"fps=30,scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H},setsar=1,setpts=PTS+{b['start']:.3f}/TB[br{i}];"
                   f"[{prev}][br{i}]overlay=eof_action=pass:enable='between(t,{b['start']:.3f},{b['end']:.3f})'[o{i}];")
            prev = f"o{i}"
        fc = fc.rstrip(";")
        overlaid = os.path.join(tmp, "overlaid.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc,
             "-map", f"[{prev}]", "-map", "0:a", *venc,
             "-c:a", "copy", overlaid])
        os.makedirs(os.path.dirname(out_mp4) or ".", exist_ok=True)
        os.replace(overlaid, out_mp4)
    else:
        # Zielordner anlegen: fehlt er (z.B. work/<name>/bench/), wirft
        # os.replace einen FileNotFoundError, der so aussieht, als fehle die
        # Quelldatei — der komplette Render war dann umsonst (29.07., v5m).
        os.makedirs(os.path.dirname(out_mp4) or ".", exist_ok=True)
        os.replace(base, out_mp4)

    # 6. B-Roll-SYNC-Pass (broll_sync.json im Workdir): parallel aufgenommenes
    #    Screen-Recording quellzeit-gekoppelt + Facecam-Kreis. Wird bei JEDEM
    #    Render angewendet (auch Cockpit-Re-Render), Mapping rechnet immer aus
    #    den aktuellen effektiven Segmenten -> ueberlebt Re-Cuts.
    sync_cfg = os.path.join(workdir, "broll_sync.json")
    segs_eff = os.path.join(workdir, "segments_effective.json")
    # broll_sync_pass.py ist ein Longform-Werkzeug (B-Roll ueber mehrere
    # Aufnahmen synchronisieren) und in dieser Shortform-Fassung nicht
    # enthalten. Ohne die Datei wird der Pass still uebersprungen.
    _sync_bin = os.path.join(HERE, "broll_sync_pass.py")
    if os.path.exists(sync_cfg) and os.path.exists(segs_eff) and os.path.exists(_sync_bin):
        tmp_sync = out_mp4 + ".sync.mp4"
        r = run([sys.executable, os.path.join(HERE, "broll_sync_pass.py"),
                 out_mp4, segs_eff, sync_cfg, tmp_sync, "--mode", mode])
        if r.stdout:
            print(r.stdout.rstrip(), flush=True)   # [sync]-Zeilen ins Log durchreichen
        os.replace(tmp_sync, out_mp4)
        print("[rerender] B-Roll-SYNC-Pass angewendet", flush=True)
    print(f"[rerender] FERTIG -> {out_mp4}", flush=True)

if __name__ == "__main__":
    main()
