#!/usr/bin/env python3
"""Externes Mikrofon millisekundengenau auf das Video syncen (03.09.2026).

Wird parallel mit einem externen Mikrofon aufgenommen, muss das System die
Mikrofonspur exakt auf die Kameraspur legen, danach die Kameraspur muten und
nur noch das Mikrofon verwenden.

Verfahren:
  1. beide Spuren als Mono-WAV ziehen (Kamera 16k reicht zum Finden)
  2. grober Versatz ueber die Kreuzkorrelation der Lautstaerke-Huellkurve
     (100 Hz) — robust gegen unterschiedliche Klangfarbe der Mikrofone
  3. Feinsuche auf der Wellenform bei 48 kHz -> Genauigkeit unter 1 ms
  4. DRIFT-PRUEFUNG an mehreren Fenstern ueber die ganze Laenge: getrennte
     Uhren in Kamera und Recorder laufen auseinander. Ohne diese Pruefung
     passt der Anfang und das Ende ist hundert Millisekunden daneben.

Aufruf:
  audio_sync.py <video> <mikrofon-audio> <workdir> [--nur-messen]
"""
import json, os, subprocess, sys, tempfile
import numpy as np

SR_GROB = 100          # Huellkurve
SR_FEIN = 48000


def run(cmd, label):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-2000:])
        raise SystemExit(f"[audio_sync] {label} fehlgeschlagen")
    return r


def wav_mono(quelle, ziel, sr):
    run(["ffmpeg", "-y", "-v", "error", "-i", quelle, "-vn",
         "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", ziel], f"extract {sr}")
    import wave
    with wave.open(ziel, "rb") as w:
        daten = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return daten.astype(np.float32) / 32768.0


def huellkurve(x, sr_in, sr_out):
    """RMS je Fenster — macht verschiedene Mikrofone vergleichbar."""
    schritt = max(1, int(sr_in / sr_out))
    n = len(x) // schritt
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    e = np.sqrt((x[:n * schritt].reshape(n, schritt) ** 2).mean(axis=1))
    return (e - e.mean()).astype(np.float32)


def korrelation(a, b):
    """Versatz von b gegenueber a in Samples, plus Guete."""
    n = 1
    while n < len(a) + len(b):
        n *= 2
    fa = np.fft.rfft(a, n)
    fb = np.fft.rfft(b, n)
    r = np.fft.irfft(fa * np.conj(fb), n)
    r = np.concatenate([r[-(len(b) - 1):], r[:len(a)]])
    i = int(np.argmax(r))
    spitze = float(r[i])
    rest = np.abs(r).mean()
    return i - (len(b) - 1), (spitze / rest if rest > 0 else 0.0)


def fenster_versatz(vid, mic, mitte_s, start_versatz, laenge_s=8.0, such_s=0.5):
    """Feinversatz in einem Fenster bei 48 kHz (Sub-Millisekunde).

    Konvention: mikrofon_zeit = video_zeit + versatz.
    start_versatz ist der grobe Wert aus der Huellkurve — OHNE ihn sucht die
    Feinsuche an der voellig falschen Stelle und liefert Rauschen.
    """
    a0 = int(max(0, (mitte_s - laenge_s / 2) * SR_FEIN))
    a1 = int(min(len(vid), (mitte_s + laenge_s / 2) * SR_FEIN))
    if a1 - a0 < SR_FEIN:
        return None, 0.0
    stueck = vid[a0:a1]
    mitte_mic = a0 + int(round(start_versatz * SR_FEIN))
    such0 = max(0, mitte_mic - int(such_s * SR_FEIN))
    such1 = min(len(mic), mitte_mic + len(stueck) + int(such_s * SR_FEIN))
    if such1 - such0 < len(stueck) + 100:
        return None, 0.0
    ziel = mic[such0:such1]
    v, guete = korrelation(ziel - ziel.mean(), stueck - stueck.mean())
    return (such0 + v - a0) / SR_FEIN, guete


def main():
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    video, audio, workdir = sys.argv[1], sys.argv[2], os.path.abspath(sys.argv[3])
    os.makedirs(workdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="sync_")

    # Die Mikrofon-Extrakte landen direkt im Workdir und heissen so, wie die
    # Schnitt-Kette sie erwartet. Vorher zog erst der Sync beide Spuren je
    # zweimal und danach der Cutter dieselben nochmal aus dem Master —
    # sechs Durchlaeufe durch grosse Dateien fuer Daten, die schon dalagen.
    print("[audio_sync] Spuren ziehen …", flush=True)
    v16 = wav_mono(video, os.path.join(tmp, "v16.wav"), 16000)
    m16 = wav_mono(audio, os.path.join(tmp, "m16.wav"), 16000)
    print(f"[audio_sync] Kamera {len(v16)/16000:.1f}s | Mikrofon {len(m16)/16000:.1f}s", flush=True)

    hv = huellkurve(v16, 16000, SR_GROB)
    hm = huellkurve(m16, 16000, SR_GROB)
    # Konvention ueberall: mikrofon_zeit = video_zeit + versatz
    grob, guete = korrelation(hm, hv)
    grob_s = grob / SR_GROB
    print(f"[audio_sync] grober Versatz {grob_s:+.3f}s (Guete {guete:.1f})", flush=True)

    print("[audio_sync] Feinsuche bei 48 kHz …", flush=True)
    v48 = wav_mono(video, os.path.join(tmp, "v48.wav"), SR_FEIN)
    m48 = wav_mono(audio, os.path.join(tmp, "m48.wav"), SR_FEIN)

    dauer_v = len(v48) / SR_FEIN
    proben = [p for p in (dauer_v * f for f in (0.12, 0.35, 0.5, 0.68, 0.88))
              if 10 < p < dauer_v - 10]
    messungen = []
    for p in proben:
        v, g = fenster_versatz(v48, m48, p, grob_s)
        if v is not None and g > 4:
            messungen.append((p, v, g))
            print(f"  bei {p:7.1f}s: Versatz {v:+.4f}s (Guete {g:.1f})", flush=True)

    if not messungen:
        raise SystemExit("[audio_sync] kein verlaesslicher Versatz gefunden — "
                         "stimmen Video und Audio zusammen?")

    zeiten = np.array([m[0] for m in messungen])
    werte = np.array([m[1] for m in messungen])
    versatz = float(np.median(werte))
    drift_ms = float((werte.max() - werte.min()) * 1000)
    if len(messungen) >= 3:
        steigung = float(np.polyfit(zeiten, werte, 1)[0])
    else:
        steigung = 0.0
    ppm = steigung * 1e6

    print(f"\n[audio_sync] Versatz (Median): {versatz:+.4f}s")
    print(f"[audio_sync] Streuung ueber die Aufnahme: {drift_ms:.1f} ms")
    print(f"[audio_sync] Uhren-Drift: {ppm:+.1f} ppm "
          f"({steigung*3600*1000:+.0f} ms pro Stunde)")
    if drift_ms > 60:
        print("[audio_sync] ⚠ Drift ueber 60 ms — reines Verschieben reicht nicht, "
              "das Mikrofon muss zusaetzlich gedehnt werden (atempo/asetrate).")
    else:
        print("[audio_sync] Drift unkritisch — reines Verschieben genuegt.")

    def f(x):
        return round(float(x), 5)
    doc = {"video": video, "audio": audio,
           "versatz_s": round(versatz, 5),
           "drift_ms": round(drift_ms, 2),
           "drift_ppm": round(ppm, 2),
           "messungen": [{"bei_s": f(t), "versatz_s": f(v),
                          "guete": f(g)} for t, v, g in messungen],
           "dauer_video_s": round(len(v48) / SR_FEIN, 3),
           "dauer_audio_s": round(len(m48) / SR_FEIN, 3)}
    p = os.path.join(workdir, "audio_sync.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"[audio_sync] -> {p}")

    if "--nur-messen" in sys.argv:
        return

    # --- anwenden: Master mit Mikrofon-Ton bauen ---------------------------
    # Kamera-Ton wird komplett ersetzt, nicht gemischt. Der Videostream wird
    # nur kopiert (kein Neu-Encodieren), das dauert ein bis zwei Minuten
    # statt einer Stunde.
    ziel = os.path.join(workdir, "master_mic.mov")
    delay_ms = int(round(versatz * -1000))     # versatz ist negativ, wenn das
                                               # Mikrofon spaeter gestartet hat
    if delay_ms >= 0:
        af = f"adelay={delay_ms}|{delay_ms}"
        print(f"[audio_sync] Mikrofon um {delay_ms} ms nach hinten schieben", flush=True)
        ein = ["-i", video, "-i", audio]
        filt = ["-filter_complex", f"[1:a]{af},aresample=48000[aout]"]
    else:
        # Mikrofon lief frueher los -> vorne abschneiden
        ab = abs(versatz)
        print(f"[audio_sync] Mikrofon vorne um {ab:.3f}s kuerzen", flush=True)
        ein = ["-i", video, "-ss", f"{ab:.5f}", "-i", audio]
        filt = ["-filter_complex", "[1:a]aresample=48000[aout]"]

    tmp_ziel = ziel + ".part"
    # -f mov ist Pflicht: ffmpeg leitet das Format aus der Endung ab und
    # kennt ".part" nicht (dieselbe Falle wie beim All-Intra-Proxy).
    run(["ffmpeg", "-y", "-v", "error"] + ein + filt +
        ["-map", "0:v:0", "-map", "[aout]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
         "-shortest", "-f", "mov", tmp_ziel], "Master bauen")
    os.replace(tmp_ziel, ziel)
    gr = os.path.getsize(ziel) / 1048576
    print(f"[audio_sync] Master mit Mikrofon-Ton: {ziel} ({gr:.0f} MB)")
    print("[audio_sync] Kamera-Ton ist ersetzt, nicht gemischt.")

    # --- Arbeitsdateien gleich mitliefern -----------------------------------
    # audio48k/16k zieht die Schnitt-Kette sonst selbst aus dem Master; hier
    # kosten sie fast nichts, weil das Mikrofon-Audio klein ist.
    for name, sr in (("audio48k.wav", 48000), ("audio16k.wav", 16000)):
        p_out = os.path.join(workdir, name)
        if os.path.exists(p_out):
            continue
        run(["ffmpeg", "-y", "-v", "error", "-i", ziel, "-vn",
             "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", p_out], f"extract {name}")
    print("[audio_sync] audio48k.wav + audio16k.wav liegen bereit", flush=True)

    # --- Playback-Proxy PARALLEL starten ------------------------------------
    # Er haengt nur am Videostream, nicht am Schnitt — er kann fertig sein,
    # bevor die Transkription durch ist. Vorher lief er ganz am Ende und
    # Das kostete frueher 12 Minuten Wartezeit.
    proxy = os.path.join(workdir, "edit_intra_main.mp4")
    if not os.path.exists(proxy):
        log = open(os.path.join(workdir, "proxy_bau.log"), "w")
        subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-y", "-i", ziel,
             "-vf", "scale=1080:-2",
             "-c:v", "libx264", "-g", "1", "-bf", "0", "-crf", "23",
             "-preset", "veryfast", "-tune", "fastdecode",
             "-r", "30", "-vsync", "cfr", "-c:a", "copy",
             "-movflags", "+faststart", "-f", "mp4", proxy + ".part"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print("[audio_sync] Playback-Proxy laeuft im Hintergrund "
              "(1080 breit, jedes Bild ein Keyframe) — Umbenennen per "
              "proxy_fertig.py, wenn er durch ist", flush=True)


if __name__ == "__main__":
    main()
