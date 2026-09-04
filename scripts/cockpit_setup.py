#!/usr/bin/env python3
"""Cockpit-Einrichtung fuer EIN Video — idempotent, verifizierend, zerstoerungsfrei.

Sorgt dafuer, dass ein Workdir ALLE Regeln erfuellt, die fuer jedes Video
gelten sollen (siehe Pruefliste unten). Laeuft am Ende von Phase D und darf
jederzeit erneut laufen: bestehende Schnitte, Slot-Positionen und Grafiken
werden NIE zurueckgesetzt.

Usage:
  cockpit_setup.py <workdir> [--src <video>] [--broll <datei> --offset <sek>] [--check-only]

Pruefliste (alles wird gesetzt ODER als Befund gemeldet):
  1  broll_sync.json vorhanden, Quelle existiert, Offset gesetzt
  2  pip.transition.dur = 0.45      (Facecam faehrt weich rein/raus)
  3  render_size = [1920,1080]      (Render immer 1080p)
  4  mirror_sources = false          sobald Screen-Slots materialisiert sind
  5  Screen-Stuecke als broll[]-Slots (kind screen, sticky, shift, anchor)
  6  Grafiken als kind free mit anchor  (nie mitgeschnitten)
  7  cockpit.json (src_video, segments, qa, port)
  8  Quellen per Hardlink geschuetzt (.protect_*)
  9  ffprobe/ffmpeg erreichbar
 10  keine Slot-Ueberlappungen in der Screen-Spur
"""
import json
import os
import shutil
import subprocess
import sys
import time

for _p in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
    if os.path.isdir(_p) and _p not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

OK, WARN, FAIL = "  OK  ", " WARN ", " FAIL "
befunde = []


def sag(status, text):
    print(f"[{status}] {text}")
    if status != OK:
        befunde.append(text)


def lade(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def sichern(pfad):
    """Zeitstempel-Backup, bevor irgendetwas geschrieben wird."""
    if not os.path.exists(pfad):
        return None
    d = os.path.join(os.path.dirname(pfad), "backups")
    os.makedirs(d, exist_ok=True)
    ziel = os.path.join(d, "%s_setup_%s.json" % (
        os.path.basename(pfad).replace(".json", ""), time.strftime("%Y%m%d_%H%M%S")))
    shutil.copy2(pfad, ziel)
    return ziel


def schreibe_atomar(pfad, obj):
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, pfad)


def dauer(f):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", f],
                       capture_output=True, text=True)
    return float((r.stdout or "0").strip() or 0)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    wd = os.path.abspath(args[0])
    check_only = "--check-only" in args
    broll_arg = args[args.index("--broll") + 1] if "--broll" in args else None
    offset_arg = float(args[args.index("--offset") + 1]) if "--offset" in args else None
    src_arg = args[args.index("--src") + 1] if "--src" in args else None
    name = os.path.basename(wd)
    print(f"\n=== Cockpit-Einrichtung: {name} ===")
    if check_only:
        print("(nur pruefen, nichts schreiben)\n")

    # --- 9. Werkzeuge -------------------------------------------------------
    for t in ("ffprobe", "ffmpeg"):
        sag(OK if shutil.which(t) else FAIL, f"{t}: {shutil.which(t) or 'NICHT GEFUNDEN'}")

    # --- 8. Quellenschutz ---------------------------------------------------
    for rolle in ("main", "broll"):
        treffer = [f for f in os.listdir(wd) if f.startswith(f".protect_{rolle}")]
        if treffer:
            p = os.path.join(wd, treffer[0])
            n = os.stat(p).st_nlink
            sag(OK if n >= 2 else WARN,
                f"Quellschutz {rolle}: {treffer[0]} ({n} Hardlinks)")
        else:
            _bs_early = lade(os.path.join(wd, "broll_sync.json")) or {}
            if rolle == "broll" and not (_bs_early.get("sources") or []):
                sag(OK, "Quellschutz broll: nicht noetig (kein B-Roll)")
            elif rolle == "main" and src_arg:
                pass  # wird gleich aus --src angelegt (Meldung kommt dort)
            else:
                sag(WARN, f"Quellschutz {rolle}: kein .protect_{rolle}.* im Workdir")

    no_broll = False
    # --- 1-4. broll_sync.json ----------------------------------------------
    bs_pfad = os.path.join(wd, "broll_sync.json")
    bs = lade(bs_pfad)
    if bs is None:
        if broll_arg and offset_arg is not None:
            bs = {"sources": [{"name": "Screen", "file": os.path.abspath(broll_arg),
                               "src_offset": offset_arg}], "pip": {}}
            sag(WARN, "broll_sync.json fehlte — aus --broll/--offset neu angelegt")
        else:
            # Reiner Talking-Head-Modus (kein Screen-/B-Roll-Material): legitim.
            bs = {"sources": [], "pip": {}}
            no_broll = True
            sag(OK, "kein B-Roll konfiguriert — reiner Talking-Head-Modus (leere broll_sync wird angelegt)")
    quellen = bs.get("sources") or []
    if not quellen:
        no_broll = True
    for q in quellen:
        sag(OK if os.path.exists(q.get("file", "")) else FAIL,
            f"B-Roll-Quelle: {os.path.basename(q.get('file',''))} @ offset {q.get('src_offset')}")

    pip = bs.setdefault("pip", {})
    aenderungen = []
    if float((pip.get("transition") or {}).get("dur", 0) or 0) != 0.45:
        pip["transition"] = {"dur": 0.45}
        aenderungen.append("pip.transition.dur=0.45")
    if bs.get("render_size") != [1920, 1080]:
        bs["render_size"] = [1920, 1080]
        aenderungen.append("render_size=1920x1080")
    if not pip.get("crop_frac"):
        if no_broll:
            sag(OK, "pip.crop_frac: nicht noetig (kein B-Roll)")
        else:
            sag(WARN, "pip.crop_frac fehlt — Facecam-Ausschnitt wurde nie vermessen!")
    else:
        cf = pip["crop_frac"]
        sag(OK, f"Facecam-Crop: x={cf.get('x')} y={cf.get('y')} size={cf.get('size_of_h')}")

    # --- 5/6. Slots ---------------------------------------------------------
    ovr_pfad = os.path.join(wd, "cockpit_overrides.json")
    ovr = lade(ovr_pfad, {}) or {}
    slots = ovr.get("broll", []) or []
    src_dateien = {q.get("file") for q in quellen}
    screen = [b for b in slots if b.get("file") in src_dateien]
    gfx = [b for b in slots if b.get("file") and b.get("file") not in src_dateien]

    fehlend_kind = [b for b in slots if b.get("file") and not b.get("kind")]
    if fehlend_kind:
        sag(WARN, f"{len(fehlend_kind)} Slot(s) ohne kind — das Cockpit heilt sie "
                  f"beim Laden selbst (positionserhaltend)")
    sag(OK if (screen or no_broll) else WARN,
        f"Screen-Slots: {len(screen)}"
        + (f" ({sum(1 for b in screen if b.get('sticky') is not False)} gekoppelt)" if screen
           else (" (kein B-Roll)" if no_broll else " — Screen klebt NICHT am Main")))
    sag(OK, f"Grafik-Slots: {len(gfx)}")
    fehlende_dateien = [b.get("name") for b in gfx if not os.path.exists(b.get("file", ""))]
    if fehlende_dateien:
        sag(FAIL, f"Grafik-Dateien fehlen: {', '.join(str(x) for x in fehlende_dateien)}")

    # 4. mirror_sources nur abschalten, wenn Screen-Slots wirklich da sind
    if screen and bs.get("mirror_sources") is not False:
        bs["mirror_sources"] = False
        aenderungen.append("mirror_sources=false")
    elif not screen and bs.get("mirror_sources") is False:
        sag(WARN, "mirror_sources=false, aber KEINE Screen-Slots — Render zeigt "
                  "dann gar kein Screen-B-Roll!")

    # 10. Ueberlappungen
    ss = sorted(screen, key=lambda b: float(b.get("start", 0)))
    ov = sum(1 for a, b in zip(ss, ss[1:])
             if float(b.get("start", 0)) < float(a.get("end", 0)) - 0.05)
    sag(OK if ov == 0 else WARN, f"Screen-Slot-Ueberlappungen: {ov}")

    # --- 7. cockpit.json ----------------------------------------------------
    ck_pfad = os.path.join(wd, "cockpit.json")
    ck = lade(ck_pfad, {}) or {}
    seg = "segments_v5_repaired.json" if os.path.exists(
        os.path.join(wd, "segments_v5_repaired.json")) else "segments_v5.json"
    qa = "verify2/qa_stage_a.json" if os.path.exists(
        os.path.join(wd, "verify2", "qa_stage_a.json")) else "qa_stage_a.json"
    src = ck.get("src_video")
    if src_arg and os.path.exists(src_arg):
        src = os.path.abspath(src_arg)
        # Quellschutz: Hardlink ins Workdir (schuetzt vor versehentlichem Loeschen
        # der Quelle; klappt nur auf demselben Volume — sonst ehrliche Warnung)
        schutz = os.path.join(wd, ".protect_main" + os.path.splitext(src)[1])
        if not os.path.exists(schutz):
            try:
                os.link(src, schutz)
                sag(OK, f"Quellschutz main angelegt: {os.path.basename(schutz)}")
            except OSError:
                sag(WARN, "Quellschutz main: Hardlink nicht moeglich (anderes Volume?)")
    if not src or not os.path.exists(src):
        kand = [f for f in os.listdir(wd) if f.startswith(".protect_main")]
        src = os.path.join(wd, kand[0]) if kand else src
    soll = {"src_video": src, "segments": seg, "qa": qa, "port": 8766}
    if ck != soll:
        ck_neu = soll
        aenderungen.append("cockpit.json aktualisiert")
    else:
        ck_neu = ck
    sag(OK if src and os.path.exists(src) else FAIL,
        f"cockpit.json: src={os.path.basename(src or '?')} segments={seg} qa={qa}")

    # --- 11: Pausen-Karte (Echo-Clamp-Grundlage, 01.09.) --------------------
    # Ohne Karte klemmt weder Player noch Renderer die L-Cuts an die
    # gemessene Stille -> Echo-/Kapp-Risiko an Split-Naehten.
    pm_pfad = os.path.join(wd, "pause_map.json")
    wav16 = os.path.join(wd, "audio16k.wav")
    if os.path.exists(pm_pfad):
        sag(OK, "pause_map.json vorhanden (L-Cut-Echo-Clamp aktiv)")
    elif os.path.exists(wav16):
        if check_only:
            sag(WARN, "pause_map.json fehlt — wuerde jetzt aus audio16k.wav gemessen")
        else:
            try:
                subprocess.run(
                    [sys.executable,
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "pause_map.py"),
                     wd], check=True, timeout=900)
                sag(OK, "pause_map.json gemessen (Waveform-Pausen)")
            except Exception as e:  # noqa: BLE001
                sag(WARN, f"pause_map.py fehlgeschlagen: {e!r}")
    else:
        sag(WARN, "audio16k.wav fehlt — Pausen-Karte nicht messbar, L-Cuts werden "
                  "NICHT an die Stille geklemmt")

    # --- 12: Playback-Proxy (fluessiges Naht-Playback) ----------------------
    if os.path.exists(os.path.join(wd, "edit_intra_main.mp4")):
        sag(OK, "edit_intra_main.mp4 vorhanden (Naht-Seeks ~1 Frame)")
    else:
        sag(WARN, "edit_intra_main.mp4 FEHLT — Wiedergabe ruckelt an jeder Naht. "
                  "Bauen (15-60 Min, im Hintergrund): "
                  "python scripts/build_playback_proxy.py " + os.path.basename(wd))

    # --- 13: Status-Hooks der persistenten Agent-Session --------------------
    # (tmux-Sessions ueberleben Projektwechsel; die Hooks machen den Status
    #  "arbeitet/fertig" im Cockpit-Badge sichtbar. Nur POSIX — unter Windows
    #  laeuft das Terminal im klassischen Modus ohne Persistenz.)
    if os.name != "nt":
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _spfad = os.path.join(_root, ".claude", "settings.json")
        try:
            _s = lade(_spfad, {}) or {}
            _hk = _s.setdefault("hooks", {})
            _cmd = "\"$CLAUDE_PROJECT_DIR\"/scripts/cutter_session_hook.py"
            def _hat_hook(_lst):
                # NICHT ueber json.dumps vergleichen — das escapt die Quotes
                # im Kommando und der Substring matcht nie (haette die Hooks
                # bei jedem Lauf dupliziert; im Test gefunden).
                for _e in _lst:
                    for _h in (_e.get("hooks") or []):
                        if "cutter_session_hook" in str(_h.get("command", "")):
                            return True
                return False
            _neu = []
            for _ev in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
                _lst = _hk.setdefault(_ev, [])
                if not _hat_hook(_lst):
                    _lst.append({"hooks": [{"type": "command", "command": _cmd}]})
                    _neu.append(_ev)
            if _neu and not check_only:
                os.makedirs(os.path.dirname(_spfad), exist_ok=True)
                schreibe_atomar(_spfad, _s)
                sag(OK, "Agent-Status-Hooks registriert (%s) — greifen ab der "
                        "naechsten Claude-Session" % ", ".join(_neu))
            elif _neu:
                sag(WARN, "Agent-Status-Hooks fehlen (%s) — wuerden registriert" % ", ".join(_neu))
            else:
                sag(OK, "Agent-Status-Hooks registriert (Session-Status im Badge)")
        except Exception as e:  # noqa: BLE001
            sag(WARN, f"Hook-Registrierung uebersprungen: {e!r}")
    else:
        sag(WARN, "Windows: persistente Agent-Sessions (tmux) nicht verfuegbar — "
                  "Terminal laeuft klassisch, Auftraege enden mit dem Cockpit")

    # --- schreiben ----------------------------------------------------------
    if aenderungen and not check_only:
        b1 = sichern(bs_pfad); b2 = sichern(ck_pfad)
        schreibe_atomar(bs_pfad, bs)
        schreibe_atomar(ck_pfad, ck_neu)
        print(f"\n  geaendert: {', '.join(aenderungen)}")
        if b1 or b2:
            print(f"  Backups: {', '.join(x for x in (b1, b2) if x)}")
        print("  (cockpit_overrides.json wurde NICHT angefasst — Schnitte bleiben)")
    elif aenderungen:
        print(f"\n  wuerde aendern: {', '.join(aenderungen)}")
    else:
        print("\n  nichts zu aendern")
    print_bilanz()


def print_bilanz():
    print()
    if befunde:
        print(f"=== {len(befunde)} Befund(e) ===")
        for b in befunde:
            print("  - " + b)
        sys.exit(2)
    print("=== Alles eingerichtet ===")


if __name__ == "__main__":
    main()
