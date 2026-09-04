#!/usr/bin/env python3
"""Pausen-Karte fuer den L-Cut-Silence-Clamp (01.09.2026).

Misst ALLE Sprechpausen der WAV per Waveform (AudioMap: Otsu + 5ms-Fein-Runs)
und schreibt sie nach work/<name>/pause_map.json. Cockpit-Player und Renderer
klemmen damit jeden J-Cut auf die tatsaechlich vorhandene Stille an der Naht —
NIE auf ASR-Wortzeiten (Gesetz "pausen_immer_messen": ASR meldet in
Retake-Ketten 0ms, die Waveform 330-503ms; umgekehrt luegt sie auch).

Hintergrund (L-Cut-Stotterer yt1b, 01.09.): jcut war fest 0,25s, die echte
Naht-Stille im Median 0,12s -> die Roll-Semantik (30.08.) kappte bei 79% der
95 Naehte das letzte Wort mitten im Phonem. Der Clamp macht jcut > Stille
unmoeglich; damit kann auch die Ueberlapp-Wiedergabe keinen Doppel-Ton mehr
erzeugen (es ueberlappt nur noch Stille mit Sprache).

ACHSE: Die WAV muss auf der PRAESENTATIONSACHSE liegen (wav_dur ==
audio_start + audio_dur der Quelle, wie vom audioAligned-Guard geprueft).
Dann sind die Pausen-Zeiten direkt file-/player-kompatibel. Bei nicht
alignierter WAV wird "aligned": false vermerkt und der audio_start der
Quelle mitgeschrieben, damit Konsumenten korrigieren koennen.

Aufruf:
  .venv312/bin/python scripts/pause_map.py work/<name> [--wav pfad] [--src pfad]
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_measure import AudioMap  # noqa: E402

MIN_PAUSE_S = 0.08   # auch kurze Luecken erfassen — der Clamp braucht sie


def probe_audio_start(src):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,start_time", "-of", "csv=p=0", src],
            text=True).strip().splitlines()
        for line in out:
            p = line.split(",")
            if len(p) >= 2 and p[0] == "audio":
                return float(p[1])
    except Exception:
        pass
    return None


def probe_dur(path, what="format=duration"):
    try:
        return float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", what,
             "-of", "csv=p=0", path], text=True).strip().splitlines()[0])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--wav", default=None, help="WAV (default: <workdir>/audio16k.wav)")
    ap.add_argument("--src", default=None, help="Quellvideo fuer den Alignment-Check")
    args = ap.parse_args()

    w = os.path.abspath(args.workdir)
    wav = args.wav or os.path.join(w, "audio16k.wav")
    if not os.path.exists(wav):
        sys.exit(f"FEHLER: {wav} fehlt — erst Audio extrahieren (aligniert!).")

    # Alignment-Check (gleiche Logik wie der Server-Guard, Schwelle 0.10)
    src = args.src
    if not src:
        cj = os.path.join(w, "cockpit.json")
        if os.path.exists(cj):
            src = (json.load(open(cj)) or {}).get("src_video")
    aligned, audio_start = None, None
    if src and os.path.exists(src):
        audio_start = probe_audio_start(src)
        wav_dur = probe_dur(wav)
        a_dur = None
        try:
            for line in subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_type,duration", "-of", "csv=p=0", src],
                    text=True).strip().splitlines():
                p = line.split(",")
                if len(p) >= 2 and p[0] == "audio":
                    a_dur = float(p[1])
        except Exception:
            pass
        if wav_dur is not None and a_dur is not None and audio_start is not None:
            aligned = abs(wav_dur - (audio_start + a_dur)) < 0.10

    print(f"[pause_map] vermesse {os.path.basename(wav)} "
          f"(min_pause {MIN_PAUSE_S*1000:.0f}ms) ...", flush=True)
    am = AudioMap(wav, min_pause_s=MIN_PAUSE_S)

    # Verfeinerung: echte Sprach-Kanten (5ms-Runs) statt grober Otsu-Grenzen.
    pauses = []
    for p in am.pauses:
        s = am.speech_offset(p)   # letztes echtes Sprach-Ende vor der Pause
        e = am.speech_onset(p)    # erster echter Sprach-Beginn danach
        if e - s >= MIN_PAUSE_S * 0.75:
            pauses.append([round(s, 3), round(e, 3)])

    doc = {
        "generated": "pause_map.py",
        "wav": os.path.basename(wav),
        "threshold_db": round(am.thr, 2),
        "min_pause_s": MIN_PAUSE_S,
        "aligned": aligned,           # True: Zeiten == Praesentationsachse
        "audio_start": audio_start,   # start_time der Quelle (Korrektur-Basis)
        "n": len(pauses),
        "pauses": pauses,             # [[sprach_ende, sprach_beginn], ...]
    }
    out = os.path.join(w, "pause_map.json")
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, out)
    print(f"[pause_map] {len(pauses)} Pausen -> {out} "
          f"(thr {am.thr:.1f} dB, aligned={aligned})", flush=True)
    if aligned is False:
        print("[pause_map] ⚠ WAV NICHT praesentations-aligniert — Zeiten sind "
              f"WAV-Achse; Konsumenten muessen audio_start {audio_start} addieren.",
              flush=True)


if __name__ == "__main__":
    main()
