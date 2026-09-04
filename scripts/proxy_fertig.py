#!/usr/bin/env python3
"""Wartet auf den parallel gebauten Playback-Proxy und benennt ihn um.

Der Proxy laeuft seit 04.09. parallel zum Schnitt (audio_sync.py startet ihn).
Solange er ".part" heisst, ruehrt ihn der Cockpit-Server nicht an — sonst
mappt er eine halbfertige Datei als Playback-Quelle und der Player bleibt
leer. Dieses Skript wartet, prueft die Dauer gegen die Quelle und benennt
dann um.

Aufruf:  proxy_fertig.py <workdir> [--warte-max-sekunden N]
"""
import os, subprocess, sys, time


def dauer(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=nw=1:nk=1", p],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    w = os.path.abspath(sys.argv[1])
    max_s = 1800
    if "--warte-max-sekunden" in sys.argv:
        max_s = int(sys.argv[sys.argv.index("--warte-max-sekunden") + 1])
    ziel = os.path.join(w, "edit_intra_main.mp4")
    teil = ziel + ".part"
    quelle = os.path.join(w, "master_mic.mov")
    if not os.path.exists(quelle):
        quelle = os.path.join(w, ".protect_main.mov")

    if os.path.exists(ziel):
        print("[proxy] liegt schon vor")
        return
    start = time.time()
    letzte = -1.0
    while time.time() - start < max_s:
        if not os.path.exists(teil):
            print("[proxy] kein Proxy-Bau gefunden — laeuft er?")
            return
        gr = os.path.getsize(teil)
        time.sleep(10)
        if os.path.getsize(teil) == gr and gr > 0:
            break                      # waechst nicht mehr -> fertig
        letzte = gr
    d_soll, d_ist = dauer(quelle), dauer(teil)
    if d_soll and abs(d_ist - d_soll) > 1.0:
        print(f"[proxy] ⚠ Dauer weicht ab: {d_ist:.1f}s gegen {d_soll:.1f}s — nicht umbenannt")
        return
    os.replace(teil, ziel)
    print(f"[proxy] fertig: {os.path.getsize(ziel)/1048576:.0f} MB, {d_ist:.1f}s")


if __name__ == "__main__":
    main()
