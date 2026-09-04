#!/usr/bin/env python3
"""Cut-Cockpit — lokaler Review-Server fuer den V5-Video-Cutter.

Start:
    .venv312/bin/python scripts/cockpit_server.py <workdir> <segments_json> <qa_json> [port=8766]

Serviert ein self-contained Frontend (scripts/cockpit/index.html), liest die
Cut-Entscheidungen + QA-Report und schreibt Nach-Cut-Overrides atomar zurueck.
Nur Python-Stdlib + numpy/soundfile.
"""
import base64
import fcntl
import json
import os
import pty
import queue
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
# ---------------------------------------------------------------------------
# PATH-Reparatur (28.07.2026) — MUSS vor jedem subprocess-Aufruf stehen.
# Das Obsidian-Plugin startet uns mit PATH=/usr/bin:/bin:/usr/sbin:/sbin.
# Darin fehlt ffmpeg/ffprobe (Homebrew). Folge frueher: broll_sync_info()
# lief in FileNotFoundError, ein stilles `except: return None` machte daraus
# brollSync=null — und damit war die Sticky-B-Roll-Kopplung, die
# Composite-Preview UND "Neu rendern" tot, ohne jede Fehlermeldung.
# der Nutzer sah nur "die Regeln sind schon wieder weg".
#
# 31.07.2026 — dieselbe Falle nochmal, diesmal fuer das eingebettete Terminal:
# `claude` liegt unter ~/.npm-global/bin und stand NICHT in dieser Liste. Der
# PTY startete `zsh -lc "claude …"`, und weil eine nicht-interaktive
# Login-Shell zwar .zprofile, aber NICHT .zshrc liest (dort steht der
# npm-global-Pfad), endete das in `command not found: claude`. Der Prozess
# starb sofort, hinterliess einen Zombie, und im Cockpit blieb die rechte
# Spalte einfach leer.
_HOME = os.path.expanduser("~")
for _p in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
           "/opt/homebrew/sbin",
           os.path.join(_HOME, ".npm-global/bin"),
           os.path.join(_HOME, ".local/bin"),
           os.path.join(_HOME, "bin")):
    if os.path.isdir(_p) and _p not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")


def tool_health():
    """Sind die externen Werkzeuge erreichbar? Wird beim Start geloggt und
    ueber /api/state ans Frontend gemeldet (rotes Banner statt stiller Ausfall)."""
    out = {}
    for t in ("ffprobe", "ffmpeg", "claude"):
        out[t] = shutil.which(t)
    return out


from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "cockpit", "index.html")

# ---------------------------------------------------------------------------
# Globale Konfiguration (aus argv gesetzt in main())
# ---------------------------------------------------------------------------
CFG = {
    "workdir": None,
    "segments_path": None,
    "qa_path": None,
    "overrides_path": None,
    "proxy_path": None,
    "audio_path": None,
    "words_path": None,
    "decisions_path": None,
    "port": 8766,
}

# Lock, damit paralleles Lesen/Schreiben der Overrides sicher ist
_OVR_LOCK = threading.Lock()
# Lock um Render-Job-Check+Start (Doppel-POST-Race, Review-Befund #3)
JOB_LOCK = threading.Lock()

# Audio wird lazy + gecacht geladen (48k mono)
_AUDIO = {"data": None, "sr": None}
_AUDIO_LOCK = threading.Lock()

# Waveform-Peaks (Timeline): einmal berechnen, in-memory + Disk-Cache
_PEAKS = {"data": None, "mtime": None}
_PEAKS_LOCK = threading.Lock()
PEAKS_SR_EFF = 50  # Peak-Paare pro Sekunde


def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_audio():
    """Laedt audio48k.wav einmalig als float32 numpy-Array (mono)."""
    with _AUDIO_LOCK:
        if _AUDIO["data"] is None:
            data, sr = sf.read(CFG["audio_path"], dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            _AUDIO["data"] = data
            _AUDIO["sr"] = sr
    return _AUDIO["data"], _AUDIO["sr"]


def read_overrides():
    ovr = load_json(CFG["overrides_path"], None)
    if not isinstance(ovr, dict):
        ovr = {}
    ovr.setdefault("timeline_clips", [])
    ovr.setdefault("nudges", {})
    ovr.setdefault("gains", {})
    ovr.setdefault("broll", [])
    ovr.setdefault("broll_sync_off", [])   # Clip-IDs ohne Sync-B-Roll-Overlay
    ovr.setdefault("broll_sync_holes", []) # [[srcA,srcB],...] Ausblend-Fenster (Source-Zeit)
    ovr.setdefault("broll_sync_converted", [])  # Sync-Quellen-Namen, die in freie Slots umgewandelt wurden
    ovr.setdefault("extra_cut_word_ids", [])
    ovr.setdefault("uncut_word_ids", [])
    ovr.setdefault("deleted_segments", [])
    ovr.setdefault("splits", [])
    return ovr


_BROLL_SYNC = {"info": None, "mtime": None, "error": None}


def file_key(path):
    """Stabiler Schluessel aus dem Dateipfad (djb2, 32 Bit, base36).

    MUSS bitgleich zu fileKey() in cockpit/index.html sein — dort wird die
    Media-URL damit gebildet. Frueher lief das ueber den Listen-Index; beim
    Loeschen eines Slots verschob sich die Liste und jeder folgende Slot
    lieferte die Datei seines Vorgaengers.
    """
    h = 5381
    for ch in path:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    if h == 0:
        return "0"
    ziffern = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while h:
        h, r = divmod(h, 36)
        out = ziffern[r] + out
    return out


def broll_sync_info():
    """broll_sync.json des Workdirs + Quell-Dauern (ffprobe, gecacht).

    Multi-Source (saas7+): {"sources":[{name,file,src_offset,window?},...]}.
    Legacy-Single ({"file","src_offset"}) wird konvertiert. Liefert
    {"sources":[...], "pip":{...}} + Legacy-Top-Level-Felder (erste Quelle)
    fuer aeltere Frontends.
    """
    path = os.path.join(CFG["workdir"], "broll_sync.json")
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    if _BROLL_SYNC["info"] is not None and _BROLL_SYNC["mtime"] == mtime:
        return _BROLL_SYNC["info"]
    try:
        cfg = json.load(open(path))
        raw = cfg.get("sources")
        if raw is None:
            raw = [{"name": None, "file": cfg.get("file"),
                    "src_offset": cfg.get("src_offset", 0)}]
        sources = []
        for s in raw:
            f = s.get("file")
            if not f or not os.path.exists(f):
                continue
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "format=duration", "-of", "csv=p=0", f],
                               capture_output=True, text=True)
            dur = float((r.stdout or "0").strip() or 0)
            off = float(s.get("src_offset", 0))
            win = s.get("window") or [off, off + dur]
            pos = len(sources)
            sources.append({
                "i": pos, "file": f,
                "name": s.get("name") or os.path.basename(f),
                "src_offset": off, "duration": dur,
                "win_a": max(float(win[0]), off),
                "win_b": min(float(win[1]), off + dur),
                "url": f"/media/broll_{pos}.mp4",
            })
        if not sources:
            # Zwei sehr verschiedene Faelle, die frueher beide als "kaputt"
            # gemeldet wurden:
            #  a) Quellen sind DEKLARIERT, die Dateien fehlen -> echter Fehler
            #  b) Es sind bewusst KEINE Quellen eingetragen (Projekt hat nur
            #     Grafik-Slots, z.B. v8d) -> voellig in Ordnung, kein Banner
            deklariert = [s.get("file") for s in raw if s.get("file")]
            if deklariert:
                _BROLL_SYNC["error"] = "Quelldatei nicht gefunden: " + ", ".join(
                    os.path.basename(x) for x in deklariert[:3])
                return None
            info = {"sources": [], "pip": cfg.get("pip", {}),
                    "file": None, "name": None, "src_offset": 0, "duration": 0}
            _BROLL_SYNC["info"] = info
            _BROLL_SYNC["mtime"] = mtime
            _BROLL_SYNC["error"] = None
            return info
        first = sources[0]
        info = {"sources": sources, "pip": cfg.get("pip", {}),
                # Legacy-Felder (erste Quelle) fuer Rueckwaertskompatibilitaet:
                "file": first["file"], "name": first["name"],
                "src_offset": first["src_offset"], "duration": first["duration"]}
        _BROLL_SYNC["info"] = info
        _BROLL_SYNC["mtime"] = mtime
        return info
    except Exception as e:
        # NIE stumm scheitern — genau das hat am 27./28.07. tagelang die
        # Sticky-Kopplung lahmgelegt, ohne dass irgendwo etwas zu sehen war.
        import traceback
        print("[cockpit] FEHLER beim Laden von broll_sync.json: %r" % (e,), flush=True)
        traceback.print_exc()
        _BROLL_SYNC["error"] = "%s: %s" % (type(e).__name__, e)
        return None


def write_overrides_atomic(ovr):
    """Schreibt Overrides atomar (tmp + os.replace) — mit Versionshistorie.

    Am 29.07. ging der Slot-Stand von v8d verloren (209 -> 1) und es gab NICHTS
    zum Zurueckholen: der Server ueberschrieb den Vorstand ersatzlos. Seitdem
    wandert jede Fassung vor dem Ueberschreiben nach backups/hist/ (die letzten
    40), und ein Save, der massiv Material entfernt, bekommt zusaetzlich eine
    deutlich benannte Kopie.
    """
    path = CFG["overrides_path"]
    tmp = path + ".tmp"
    with _OVR_LOCK:
        # 1) Vorstand sichern
        try:
            if os.path.exists(path):
                hist = os.path.join(os.path.dirname(path), "backups", "hist")
                os.makedirs(hist, exist_ok=True)
                alt = load_json(path, {}) or {}
                a_slots, a_clips = len(alt.get("broll") or []), len(alt.get("timeline_clips") or [])
                n_slots, n_clips = len(ovr.get("broll") or []), len(ovr.get("timeline_clips") or [])
                stamp = time.strftime("%Y%m%d_%H%M%S")
                shutil.copy2(path, os.path.join(hist, "ovr_%s.json" % stamp))
                # 2) Auffaelliger Verlust -> eigene, gut auffindbare Kopie + Log
                drastisch = (a_slots >= 10 and n_slots < a_slots * 0.5) or \
                            (a_clips >= 10 and n_clips < a_clips * 0.5)
                if drastisch:
                    warn = os.path.join(os.path.dirname(path),
                                        "backups", "VORHER_GROSSER_VERLUST_%s.json" % stamp)
                    shutil.copy2(path, warn)
                    print("[cockpit] ACHTUNG: Save entfernt viel Material "
                          "(Slots %d->%d, Clips %d->%d). Vorstand gesichert: %s"
                          % (a_slots, n_slots, a_clips, n_clips, warn), flush=True)
                # 3) Historie begrenzen
                dateien = sorted(os.listdir(hist))
                for f in dateien[:-40]:
                    try:
                        os.remove(os.path.join(hist, f))
                    except OSError:
                        pass
        except Exception as e:
            print("[cockpit] Backup vor dem Speichern fehlgeschlagen: %r" % (e,), flush=True)
        # 4) eigentliches Schreiben
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ovr, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def seg_edges(seg_idx, segments, ovr):
    """Effektive in/out-Zeiten eines Segments inkl. aktueller Nudges (Sekunden)."""
    seg = segments[seg_idx]
    din = float(ovr["nudges"].get(f"{seg_idx}:in", 0.0) or 0.0)
    dout = float(ovr["nudges"].get(f"{seg_idx}:out", 0.0) or 0.0)
    return seg["in"] + din, seg["out"] + dout


def compute_joint_times(segments, ovr):
    """Proxy-Zeit jeder Naht k = kumulierte Dauer der Segmente 0..k (inkl. Nudges).

    Naht k liegt zwischen Segment k und k+1, also am Ende von Segment k.
    Rueckgabe: Liste von n_segments-1 Proxy-Zeiten (Sekunden).
    """
    cum = 0.0
    joints = []
    n = len(segments)
    for i in range(n):
        ins, outs = seg_edges(i, segments, ovr)
        dur = max(0.0, outs - ins)
        cum += dur
        if i < n - 1:
            joints.append(round(cum, 3))
    return joints


def compute_peaks():
    """Min/Max-Peaks des KOMPLETTEN Quell-Audios (Source-Zeitachse).

    Frueher wurden Peaks nur ueber die Keep-Segmente berechnet — beim Trimmen
    ueber die alte Segment-Grenze hinaus gab es fuer das zurueckgeholte
    Material keine Daten und das Frontend konnte die Waveform nur strecken.
    Source-basiert zeichnet jeder Clip einfach seinen echten [inS, outS]-
    Ausschnitt: Trim rein/raus zeigt immer das reale Audio (CapCut-Verhalten).

    Liest audio48k.wav blockweise (nie komplett in den RAM, OOM-Lesson) und
    downsampelt auf PEAKS_SR_EFF Peak-Paare/Sekunde. peaks[i] entspricht der
    Source-Zeit i/PEAKS_SR_EFF.

    Rueckgabe: {"sr_eff", "src": True, "peaks": [[min,max],…]}
    Cache: <workdir>/peaks_src_cache.json, invalidiert wenn Audio-mtime neuer
    (segment-unabhaengig — ueberlebt alle Schnitt-Aenderungen).
    """
    audio_path = CFG["audio_path"]
    cache_path = os.path.join(CFG["workdir"], "peaks_src_cache.json")
    audio_mtime = os.path.getmtime(audio_path) if os.path.exists(audio_path) else 0

    with _PEAKS_LOCK:
        # 1. In-Memory-Cache
        if _PEAKS["data"] is not None and _PEAKS["mtime"] == audio_mtime:
            return _PEAKS["data"]
        # 2. Disk-Cache (Format-Guard: nur Source-Format akzeptieren)
        if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= audio_mtime:
            try:
                data = load_json(cache_path)
                if isinstance(data, dict) and data.get("src") is True and "peaks" in data:
                    _PEAKS["data"] = data
                    _PEAKS["mtime"] = audio_mtime
                    return data
            except Exception:  # noqa: BLE001
                pass
        # 3. Frisch berechnen (blockweise, Raster exakt sr/PEAKS_SR_EFF Samples)
        peaks = []
        with sf.SoundFile(audio_path) as f:
            sr = f.samplerate
            spp = max(1, int(round(sr / PEAKS_SR_EFF)))  # Samples pro Peak (48k/50 = 960)
            block_peaks = 3000                            # ~60s Audio pro Block
            while True:
                block = f.read(spp * block_peaks, dtype="float32", always_2d=False)
                if len(block) == 0:
                    break
                if block.ndim > 1:
                    block = block.mean(axis=1)
                n = max(1, len(block) // spp)
                idx = np.arange(n) * spp
                mins = np.minimum.reduceat(block, idx)
                maxs = np.maximum.reduceat(block, idx)
                for mn, mx in zip(mins, maxs):
                    peaks.append([round(float(mn), 4), round(float(mx), 4)])
        data = {"sr_eff": PEAKS_SR_EFF, "src": True, "peaks": peaks}
        try:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as cf:
                json.dump(data, cf)
            os.replace(tmp, cache_path)
        except OSError:
            pass
        _PEAKS["data"] = data
        _PEAKS["mtime"] = audio_mtime
        return data


# ---------------------------------------------------------------------------
# Eingebettetes Claude-Terminal (PTY) — echte 1:1-TUI in der rechten Spalte.
# Ein Singleton-Terminal pro Server; SSE streamt Output (base64-Chunks),
# POST /api/term/input schreibt Keystrokes. Ring-Buffer fuer Reconnect-Replay.
# ---------------------------------------------------------------------------
TERM = {
    "fd": None,
    "pid": None,
    "buf": b"",
    "subs": [],
    "lock": threading.Lock(),
    "alive": False,
}
TERM_BUF_MAX = 400_000
TERM_AGENT = "video-cutter"

# --------------------------------------------------------------------------
# PERSISTENTE AGENT-SESSIONS (01.09.2026): claude laeuft NICHT mehr als
# direktes PTY-Kind des Servers, sondern in einer detachten tmux-Session
# pro Projekt ("cutter-<projekt>"). Das Server-PTY ist nur noch das FENSTER
# (tmux attach). Stirbt der Server (PROJEKT WECHSELN), stirbt nur der
# Attach-Client — die Session arbeitet im Hintergrund weiter (z.B. B-Roll
# fertig bauen) und wird beim naechsten Oeffnen desselben Projekts wieder
# angedockt. tmux ist zugleich das Register (tmux ls).
# Reboot-Recovery: tmux ueberlebt keinen Neustart; der SessionStart-Hook
# (cutter_session_hook.py, Projekt-Settings) schreibt die Claude-Session-ID
# nach <workdir>/claude_session.json -> Neustart mit `--resume <id>`.
# --------------------------------------------------------------------------


def tmux_session_name(workdir=None):
    import re as _re
    base = os.path.basename(os.path.abspath(workdir or CFG["workdir"]))
    return "cutter-" + _re.sub(r"[^A-Za-z0-9_-]", "_", base)


def tmux_has_session(name):
    tb = shutil.which("tmux")
    if not tb:
        return False
    try:
        # "=" erzwingt exakten Namens-Match (sonst matcht tmux Praefixe:
        # "cutter-v1" faende auch "cutter-v1b").
        return subprocess.run([tb, "has-session", "-t", "=" + name],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def tmux_ensure_session(name, cols, rows, claude_bin):
    """Legt die detachte Projekt-Session an, falls sie fehlt. True = ok."""
    tb = shutil.which("tmux")
    if not tb or not claude_bin:
        return False
    if tmux_has_session(name):
        return True
    inner = "%s --agent %s" % (shlex.quote(claude_bin), TERM_AGENT)
    sj = load_json(os.path.join(CFG["workdir"], "claude_session.json"), {}) or {}
    rid = str(sj.get("session_id") or "").strip()
    if rid and all(ch.isalnum() or ch in "-_" for ch in rid):
        # Kontext der letzten Session zurueckholen; unbekannte ID -> frisch.
        inner = ("%s --agent %s --resume %s || %s --agent %s"
                 % (shlex.quote(claude_bin), TERM_AGENT, shlex.quote(rid),
                    shlex.quote(claude_bin), TERM_AGENT))
    try:
        subprocess.run(
            [tb, "new-session", "-d", "-s", name,
             "-x", str(int(cols)), "-y", str(int(rows)),
             "-c", os.path.dirname(HERE),
             "-e", "CUTTER_WORKDIR=%s" % os.path.abspath(CFG["workdir"]),
             "-e", "CUTTER_PROJECT=%s" % os.path.basename(os.path.abspath(CFG["workdir"])),
             "-e", "TERM=xterm-256color",
             "/bin/zsh", "-lc", inner],
            check=True, capture_output=True, timeout=10)
        # tmux-Statusleiste aus: im Cockpit soll es wie ein nacktes Claude
        # aussehen, nicht wie ein Terminal-Multiplexer.
        subprocess.run([tb, "set-option", "-t", "=" + name, "status", "off"],
                       capture_output=True, timeout=5)
        print("[cockpit] Agent-Session NEU: tmux %s%s"
              % (name, " (resume %s)" % rid[:8] if rid else ""), flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        print("[cockpit] ⚠ tmux new-session fehlgeschlagen (%r) -> Direkt-PTY" % (e,),
              flush=True)
        return False


def agent_sessions_info():
    """Alle laufenden cutter-* Sessions + ihr Status (fuer Badge/Uebersicht)."""
    out = []
    tb = shutil.which("tmux")
    if not tb:
        return out
    try:
        r = subprocess.run(
            [tb, "list-sessions", "-F",
             "#{session_name}|#{session_created}|#{session_attached}"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return out
    if r.returncode != 0:
        return out
    work_root = os.path.dirname(os.path.abspath(CFG["workdir"]))
    for line in r.stdout.strip().splitlines():
        p = line.split("|")
        if not p or not p[0].startswith("cutter-"):
            continue
        proj = p[0][len("cutter-"):]
        wd = os.path.join(work_root, proj)
        st = load_json(os.path.join(wd, "agent_status.json"), {}) or {}
        out.append({
            "session": p[0], "projekt": proj,
            "attached": len(p) > 2 and p[2] not in ("", "0"),
            "aktuell": os.path.abspath(wd) == os.path.abspath(CFG["workdir"]),
            "status": st.get("status"), "ts": st.get("ts"),
            "aufgabe": st.get("aufgabe"),
        })
    return out


def term_start(cols=120, rows=32):
    with TERM["lock"]:
        if TERM["alive"]:
            return
        # ABSOLUTEN Pfad aufloesen statt auf den Shell-PATH zu hoffen: eine
        # nicht-interaktive Login-Shell liest .zprofile, aber NICHT .zshrc —
        # dort steht bei npm-global-Installationen aber der Pfad zu `claude`.
        claude_bin = shutil.which("claude")
        tmux_bin = shutil.which("tmux")
        sess = tmux_session_name()
        # PERSISTENZ: Session detached anlegen (oder vorhandene weiternutzen),
        # das PTY hier ist nur noch der Attach-Client. Faellt tmux aus,
        # laeuft der alte Direkt-PTY-Pfad als Fallback (Session stirbt dann
        # wieder mit dem Server — besser als gar kein Terminal).
        use_tmux = bool(tmux_bin) and tmux_ensure_session(sess, cols, rows, claude_bin)
        pid, fd = pty.fork()
        if pid == 0:
            # Kind: Attach-Client bzw. Fallback-claude im Login-Shell-Kontext
            os.chdir(os.path.dirname(HERE))
            os.environ["TERM"] = "xterm-256color"
            # Hooks (cutter_session_hook.py) brauchen den Projekt-Kontext auch
            # im Fallback-Pfad:
            os.environ["CUTTER_WORKDIR"] = os.path.abspath(CFG["workdir"])
            if use_tmux:
                os.execvp(tmux_bin, [tmux_bin, "attach-session", "-t", "=" + sess])
            if not claude_bin:
                # Nicht still sterben — sonst bleibt die rechte Spalte leer und
                # niemand weiss warum (genau so ist es am 31.07. passiert).
                sys.stdout.write(
                    "\r\n  claude wurde nicht gefunden.\r\n\r\n"
                    "  PATH: %s\r\n\r\n"
                    "  Erwartet z.B. unter ~/.npm-global/bin/claude.\r\n"
                    "  Cockpit-Server mit vollem PATH neu starten.\r\n" %
                    os.environ.get("PATH", ""))
                sys.stdout.flush()
                time.sleep(3600)
                os._exit(1)
            os.execvp("/bin/zsh", ["/bin/zsh", "-lc",
                                   "%s --agent %s" % (shlex.quote(claude_bin), TERM_AGENT)])
        TERM["fd"] = fd
        TERM["pid"] = pid
        TERM["buf"] = b""
        TERM["alive"] = True
        TERM["tmux"] = use_tmux
    term_resize(cols, rows)
    threading.Thread(target=_term_reader, daemon=True).start()


def _term_reader():
    fd = TERM["fd"]
    while True:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            with TERM["lock"]:
                TERM["alive"] = False
                subs = list(TERM["subs"])
            for q in subs:
                q.put(None)
            # Zombie sicher abraeumen: WNOHANG allein greift oft zu frueh
            # (das Kind ist beim leeren read noch nicht als beendet vermerkt),
            # dann bleiben <defunct>-Eintraege stehen.
            for _try in range(20):
                try:
                    if os.waitpid(TERM["pid"], os.WNOHANG)[0]:
                        break
                except (OSError, TypeError):
                    break
                time.sleep(0.05)
            return
        with TERM["lock"]:
            TERM["buf"] = (TERM["buf"] + data)[-TERM_BUF_MAX:]
            subs = list(TERM["subs"])
        for q in subs:
            q.put(data)


def term_resize(cols, rows):
    if TERM["fd"] is None:
        return
    try:
        fcntl.ioctl(TERM["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except OSError:
        pass


def term_write(data):
    if TERM["fd"] is None or not TERM["alive"]:
        return False
    try:
        os.write(TERM["fd"], data)
        return True
    except OSError:
        return False


def term_kill():
    # Beendet nur den ATTACH-Client (Fenster zu) — die tmux-Session und der
    # Claude darin laufen weiter. Fuer das echte Beenden: term_kill_session().
    with TERM["lock"]:
        pid = TERM["pid"]
        TERM["alive"] = False
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass


def term_kill_session():
    """Beendet die persistente Agent-Session dieses Projekts WIRKLICH."""
    term_kill()
    tb = shutil.which("tmux")
    sess = tmux_session_name()
    if tb:
        try:
            subprocess.run([tb, "kill-session", "-t", "=" + sess],
                           capture_output=True, timeout=5)
            print("[cockpit] Agent-Session beendet: %s" % sess, flush=True)
        except Exception:
            pass
    # BEWUSSTES Beenden -> die naechste Session soll FRISCH starten, nicht
    # die eben gekillte resumen. claude_session.json ist nur fuer die
    # Reboot-Recovery (Session starb OHNE Nutzer-Absicht) gedacht.
    try:
        os.unlink(os.path.join(CFG["workdir"], "claude_session.json"))
    except OSError:
        pass
    # Status ehrlich machen (der SessionEnd-Hook feuert bei kill-session nicht
    # zuverlaessig — SIGHUP kann Hooks abschneiden):
    try:
        p = os.path.join(CFG["workdir"], "agent_status.json")
        st = load_json(p, {}) or {}
        st.update({"status": "beendet", "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        with open(p + ".tmp", "w") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # --- Logging leiser machen ---------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("[cockpit] %s - %s\n" % (self.address_string(), fmt % args))

    # --- kleine Helfer -----------------------------------------------------
    def _send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type, status=HTTPStatus.OK, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, content_type="text/plain; charset=utf-8", status=HTTPStatus.OK):
        self._send_bytes(text.encode("utf-8"), content_type, status)

    # --- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if route == "/" or route == "/index.html":
                return self.serve_index()
            if route == "/favicon.ico":
                return self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            if route.startswith("/firstframe/"):
                return self.serve_first_frame(route)
            if route.startswith("/medien/"):
                return self.serve_medien(route)
            if route == "/api/broll_starten_status":
                return self.broll_starten(nur_status=True)
            if route == "/api/state":
                return self.serve_state()
            if route == "/api/render_status":
                return self.render_status()
            if route == "/api/joint_audio":
                return self.serve_joint_audio(qs)
            if route == "/api/peaks":
                return self.serve_peaks()
            if route == "/media/proxy.mp4":
                return self.serve_media(CFG.get("proxy_path"))
            if route == "/media/source.mp4":
                return self.serve_media(CFG.get("src_video"))
            if route == "/media/audio48k.wav":
                # Eigene, kleine Tonquelle fuer die L-Cut-Tonspuren: PCM-WAV
                # seekt byte-genau (keine GOP, kein Decoder-Reset) und haengt
                # nicht mehr an der grossen Quelldatei.
                return self.serve_media(CFG.get("audio_path"))
            if route == "/media/broll.mp4":
                info = broll_sync_info()
                return self.serve_media(info["file"] if info else None)
            if route.startswith("/media/broll_key_"):
                # Datei-Schluessel statt Listen-Index: bleibt gueltig, egal ob
                # Slots geloescht, verschoben oder noch nicht gespeichert sind.
                # Endung ist nur Dekoration (Standbild-Slots seit 03.09.).
                key = os.path.splitext(route[len("/media/broll_key_"):])[0]
                treffer = None
                for b in read_overrides().get("broll", []):
                    f = b.get("file")
                    if f and file_key(f) == key:
                        treffer = f
                        break
                if treffer is None:
                    for q in ((broll_sync_info() or {}).get("sources") or []):
                        if file_key(q["file"]) == key:
                            treffer = q["file"]
                            break
                return self.serve_nach_typ(treffer if treffer and os.path.exists(treffer) else None)
            if route.startswith("/media/broll_slotfile_") and route.endswith(".mp4"):
                # Slot-Datei nach Index in der DEDUPLIZIERTEN Datei-Liste —
                # stabil auch fuer frisch gesplittete, ungespeicherte Slots.
                try:
                    k = int(route[len("/media/broll_slotfile_"):-len(".mp4")])
                except ValueError:
                    k = -1
                files = []
                for b in read_overrides().get("broll", []):
                    f = b.get("file")
                    if f and f not in files:
                        files.append(f)
                f = files[k] if 0 <= k < len(files) else None
                return self.serve_media(f if f and os.path.exists(f) else None)
            if route.startswith("/media/broll_slot_") and route.endswith(".mp4"):
                # Freie B-Roll-Slots (overrides.broll) — Index in der Liste
                try:
                    k = int(route[len("/media/broll_slot_"):-len(".mp4")])
                except ValueError:
                    k = -1
                slots = read_overrides().get("broll", [])
                f = slots[k].get("file") if 0 <= k < len(slots) else None
                return self.serve_media(f if f and os.path.exists(f) else None)
            if route.startswith("/media/broll_") and route.endswith(".mp4"):
                info = broll_sync_info()
                try:
                    k = int(route[len("/media/broll_"):-len(".mp4")])
                except ValueError:
                    k = -1
                srcs = (info or {}).get("sources") or []
                f = srcs[k]["file"] if 0 <= k < len(srcs) else None
                return self.serve_media(f)
            if route == "/api/term/stream":
                return self.serve_term_stream()
            if route == "/api/agent_status":
                return self._send_json({"sessions": agent_sessions_info(),
                                        "projekt": os.path.basename(os.path.abspath(CFG["workdir"]))})
            if route.startswith("/vendor/"):
                return self.serve_vendor(route)
            self._send_json({"error": "not found", "path": route}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    # --- POST --------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/api/overrides":
                return self.save_overrides()
            if route == "/api/captions":
                return self.save_captions()
            if route == "/api/broll_auftrag":
                return self.broll_auftrag()
            if route == "/api/broll_starten":
                return self.broll_starten()
            if route == "/api/first_frame":
                return self.set_first_frame()
            if route == "/api/medien_upload":
                return self.medien_upload()
            if route == "/api/captions_sync":
                return self.captions_sync()
            if route == "/api/broll_request":
                return self.broll_request()
            if route == "/api/rerender":
                return self.rerender()
            if route == "/api/term/input":
                return self.term_input()
            if route == "/api/term/resize":
                return self.term_resize_route()
            if route == "/api/term/restart":
                return self.term_restart()
            if route == "/api/term/kill_session":
                return self.term_kill_session_route()
            self._send_json({"error": "not found", "path": route}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    # ---------- B-Roll-Auftrag (T-Marker) ----------
    def broll_request(self):
        """der Nutzer markiert mit T einen Bereich -> Briefing bauen, ablegen und
        als Auftrag in die laufende Claude-Terminal-Session schreiben.
        Fire-and-forget: kehrt sofort zurueck, er arbeitet weiter."""
        body = self._read_post_json()
        try:
            a = float(body.get("start")); b = float(body.get("end"))
        except (TypeError, ValueError):
            return self._send_json({"error": "start/end fehlen"}, HTTPStatus.BAD_REQUEST)
        if b <= a:
            return self._send_json({"error": "leerer Bereich"}, HTTPStatus.BAD_REQUEST)
        wunsch = (body.get("prompt") or "").strip()

        ovr = read_overrides()
        clips = ovr.get("timeline_clips") or []
        if not clips:
            seg_doc = load_json(CFG["segments_path"], {})
            clips = [{"in": x["in"], "out": x["out"]}
                     for x in (seg_doc.get("segments") or []) if x["out"] > x["in"]]

        # ACHSE MIT J-CUTS (01.09.): Die alte Kumulation (Σ out−in) ignorierte
        # jcut — bei mod4v2 (25,75s Σ jcut) traf ein T-Marker-Auftrag am
        # Videoende die Rohstelle 25,75s zu frueh, das Briefing zitierte
        # falschen Text. Jetzt rechnet dieselbe Implementierung wie der
        # Render (apply_jcut_edges inkl. Echo-Clamp) die BILD-Achse:
        # Output t im Clip i -> raw = v_in + (t − v_start).
        _axis = [{"in": float(c["in"]), "out": float(c["out"]),
                  "jcut": max(0.0, float(c.get("jcut", 0) or 0))}
                 for c in clips if float(c["out"]) - float(c["in"]) >= 0.04]
        if _axis:
            _axis[0]["jcut"] = 0.0
        try:
            import rerender as _rr
            _rr.apply_jcut_edges(_axis, pauses=_rr.load_pause_map(CFG["workdir"]))
        except Exception as _e:  # noqa: BLE001 — Fallback: Achse ohne jcut (alter Stand)
            print("[cockpit] ⚠ Briefing-Achse ohne jcut (%r)" % (_e,), flush=True)
            _cum = 0.0
            for _s in _axis:
                _s["v_in"], _s["v_out"], _s["v_start"] = _s["in"], _s["out"], _cum
                _cum += _s["out"] - _s["in"]

        def raw_ranges(oa, ob):
            res = []
            for s in _axis:
                vdur = s["v_out"] - s["v_in"]
                x, y = max(oa, s["v_start"]), min(ob, s["v_start"] + vdur)
                if y > x:
                    res.append((s["v_in"] + (x - s["v_start"]),
                                s["v_in"] + (y - s["v_start"])))
            return res

        def out_to_raw(t):
            r = raw_ranges(t, t + 0.001)
            return round(r[0][0], 3) if r else 0.0

        words = load_json(os.path.join(CFG["workdir"], "words_aai.json"), []) or []

        def text_for(oa, ob):
            out = []
            for (ra, rb) in raw_ranges(max(0.0, oa), ob):
                out += [w["text"] for w in words
                        if w["start"] >= ra - 0.01 and w["end"] <= rb + 0.01]
            return " ".join(out).strip()

        def visual_at(t):
            best = None
            for sl in ovr.get("broll", []):
                if not sl.get("file"):
                    continue
                if float(sl.get("start", 0)) <= t <= float(sl.get("end", 0)):
                    if sl.get("kind") != "screen":
                        return "Grafik '%s'" % sl.get("name", "?")
                    best = best or "Bildschirmaufnahme"
            return best or "Talking-Head (der Nutzer fullscreen)"

        total = sum(s["v_out"] - s["v_in"] for s in _axis)   # echte Output-Laenge (mit jcuts)
        tc = lambda t: "%d:%02d" % (int(t // 60), int(t % 60))
        brief = {
            "typ": "broll_auftrag",
            "erstellt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "projekt": os.path.basename(os.path.abspath(CFG["workdir"])),
            "workdir": os.path.abspath(CFG["workdir"]),
            "quellvideo": CFG.get("src_video"),
            "zielbereich": {
                "output_start": round(a, 3), "output_end": round(b, 3),
                "dauer_s": round(b - a, 3),
                "output_start_tc": tc(a), "output_end_tc": tc(b),
                "roh_bereiche": [[round(x, 3), round(y, 3)] for x, y in raw_ranges(a, b)],
                "anchor_fuer_slot": out_to_raw(a),
            },
            "gesagt_im_bereich": text_for(a, b),
            "kontext_davor_30s": text_for(a - 30, a),
            "kontext_danach_30s": text_for(b, b + 30),
            "sichtbar": {
                "davor": visual_at(max(0.0, a - 0.5)),
                "im_bereich": visual_at((a + b) / 2.0),
                "danach": visual_at(min(total, b + 0.5)),
            },
            "vorhandene_grafiken": [
                {"name": sl.get("name"), "datei": sl.get("file"),
                 "start": sl.get("start"), "end": sl.get("end")}
                for sl in ovr.get("broll", [])
                if sl.get("kind") != "screen" and sl.get("file")
            ],
            "wunsch_von_sebastian": wunsch,
            "einbau_hinweis": (
                "Fertige Szene als FREIEN Slot in cockpit_overrides.json broll[] eintragen: "
                "{kind:'free', file:<mp4>, start:%.3f, end:<start+dauer>, src_in:0, "
                "anchor:%.3f, name:'<kurz>'}. Screen-Slots NICHT anfassen. "
                "Stil: yt1-broll/src/PluginScenes.tsx (SKAILE/VSL-Look). "
                "Facecam-Freihaltezone unten rechts (x>1430, y>580) freilassen. "
                "Uebergang zum Davor/Danach sauber gestalten."
            ) % (a, out_to_raw(a)),
        }
        reqdir = os.path.join(CFG["workdir"], "broll_requests")
        os.makedirs(reqdir, exist_ok=True)
        path = os.path.join(reqdir, "req-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        with open(path, "w") as f:
            json.dump(brief, f, ensure_ascii=False, indent=1)

        kurz = (brief["gesagt_im_bereich"] or "")[:110].replace('"', "'")
        line = ("[B-ROLL-AUFTRAG] Lies %s und baue die dort beschriebene Grafik-B-Roll "
                "(Bereich %s-%s, %.1fs). Gesagt wird dort: \"%s\". %s"
                "Szene bauen, rendern, als freien Slot eintragen, dann kurz Bescheid geben."
                % (path, tc(a), tc(b), b - a, kurz,
                   ("Wunsch: %s. " % wunsch) if wunsch else ""))
        sent = term_write(line.encode("utf-8") + b"\r")
        print("[cockpit] B-Roll-Auftrag -> %s (Terminal: %s)"
              % (os.path.basename(path), "ok" if sent else "nicht erreichbar"), flush=True)
        self._send_json({"ok": True, "request": path, "terminal": bool(sent)})

    # --- Claude-Terminal + Vendor ------------------------------------------
    VENDOR_FILES = {
        "xterm.js": "application/javascript; charset=utf-8",
        "xterm.css": "text/css; charset=utf-8",
        "addon-fit.js": "application/javascript; charset=utf-8",
    }

    def serve_vendor(self, route):
        name = route[len("/vendor/"):]
        ctype = self.VENDOR_FILES.get(name)
        if ctype is None:
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        with open(os.path.join(HERE, "cockpit", "vendor", name), "rb") as f:
            self._send_bytes(f.read(), ctype, extra_headers={"Cache-Control": "no-store"})

    def _term_origin_ok(self):
        # CSRF-Schutz: fremde Webseiten im Browser duerfen den Terminal-Endpoint
        # nicht ansteuern. Same-origin (Cockpit selbst) + Origin-lose Clients ok.
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")

    def _read_post_json(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        if ln <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(ln) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def serve_term_stream(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        term_start()
        q = queue.Queue()
        with TERM["lock"]:
            replay = TERM["buf"]
            TERM["subs"].append(q)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(chunk):
                payload = base64.b64encode(chunk).decode("ascii")
                self.wfile.write(("data: %s\n\n" % payload).encode("ascii"))
                self.wfile.flush()

            if replay:
                emit(replay)
            while True:
                try:
                    data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if data is None:
                    self.wfile.write(b"event: exit\ndata: 1\n\n")
                    self.wfile.flush()
                    return
                emit(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True
            with TERM["lock"]:
                if q in TERM["subs"]:
                    TERM["subs"].remove(q)

    def term_input(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        body = self._read_post_json()
        try:
            data = base64.b64decode(body.get("data", ""))
        except (ValueError, TypeError):
            data = b""
        self._send_json({"ok": term_write(data)})

    def term_resize_route(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        body = self._read_post_json()
        term_resize(body.get("cols", 120), body.get("rows", 32))
        self._send_json({"ok": True})

    def term_restart(self):
        # Mit tmux: nur das FENSTER neu verbinden (Session laeuft weiter).
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        term_kill()
        time.sleep(0.4)
        term_start()
        self._send_json({"ok": True})

    def term_kill_session_route(self):
        # Beendet die PERSISTENTE Agent-Session (tmux + Claude) wirklich und
        # startet ein frisches Terminal-Fenster mit neuer Session.
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        term_kill_session()
        time.sleep(0.6)
        term_start()
        self._send_json({"ok": True})

    # --- Routen-Implementierung -------------------------------------------
    def serve_index(self):
        # PROJEKT-OVERRIDE (14.08.): Liegt im Workdir eine index_override.html,
        # wird DIESE ausgeliefert statt der globalen Fassung. Damit kann ein
        # einzelnes Projekt (A/B-Test, z.B. mod4v2 mit dem 05.08.-Stand) eine
        # andere Player-Version fahren, ohne die uebrigen Projekte anzufassen.
        index_path = INDEX_HTML
        override = os.path.join(CFG.get("workdir") or "", "index_override.html")
        if CFG.get("workdir") and os.path.exists(override):
            index_path = override
        with open(index_path, "rb") as f:
            data = f.read()
        # no-store: Browser darf das Cockpit-Frontend NIE cachen, sonst kommen
        # Code-Updates beim Reload nicht an (Bug 22.07.: alte Selektions-Logik
        # blieb trotz Fix haengen).
        self._send_bytes(data, "text/html; charset=utf-8",
                         extra_headers={"Cache-Control": "no-store"})

    def serve_state(self):
        words = load_json(CFG["words_path"], [])
        decisions = load_json(CFG["decisions_path"], {})
        seg_doc = load_json(CFG["segments_path"], {})
        segments = seg_doc.get("segments", []) if isinstance(seg_doc, dict) else []
        qa = load_json(CFG["qa_path"], [])
        ovr = read_overrides()
        joint_times = compute_joint_times(segments, ovr)
        # MEDIA-HOST-SPLIT (14.08., Chromium-Recherche): Chromium erlaubt 6
        # Verbindungen pro (Schema,Host,Port). Auf dem Haupt-Port haengen 4
        # Video-Elemente + B-Roll-Layer + der permanente Terminal-SSE-Stream —
        # das Budget war voll, jeder Naht-Seek wartete in der Socket-Queue.
        # Media laeuft deshalb ueber einen eigenen Port (= eigene Gruppe).
        mb = ("http://127.0.0.1:%d" % CFG["media_port"]) if CFG.get("media_port") else ""
        bs_info = broll_sync_info()
        if bs_info and mb:
            bs_info = dict(bs_info)
            if bs_info.get("sources"):
                bs_info["sources"] = [
                    dict(q, url=(mb + q["url"]) if str(q.get("url", "")).startswith("/") else q.get("url"))
                    for q in bs_info["sources"]
                ]
        state = {
            "words": words,
            "decisions": decisions,
            "segments": segments,
            "segMeta": {
                "keep_s": seg_doc.get("keep_s") if isinstance(seg_doc, dict) else None,
                "total_s": seg_doc.get("total_s") if isinstance(seg_doc, dict) else None,
                "n_segments": len(segments),
            },
            "qa": qa,
            "overrides": ovr,
            "overridesMtime": (os.path.getmtime(CFG["overrides_path"]) if os.path.exists(CFG["overrides_path"]) else None),
            "jointTimes": joint_times,
            "proxyUrl": mb + "/media/proxy.mp4",
            "sourceUrl": ((mb + "/media/source.mp4")
                          if CFG.get("src_video") and os.path.exists(CFG["src_video"])
                          else None),
            "audioUrl": ((mb + "/media/audio48k.wav")
                         if os.path.exists(CFG.get("audio_path") or "") else None),
            "audioAligned": bool(CFG.get("audio_aligned")),
            "mediaBase": mb,
            "playbackProxy": bool(CFG.get("playback_map")),
            # PAUSEN-KARTE (01.09.): waveform-gemessene Sprechpausen fuer den
            # L-Cut-Silence-Clamp im Player (pause_map.py). Ohne Karte klemmt
            # der Player J-Cuts NICHT an die Stille — Banner weist darauf hin.
            "pauseMap": load_json(os.path.join(CFG["workdir"], "pause_map.json"), None),
            # PERSISTENTE AGENT-SESSIONS (01.09.): laufende cutter-* Sessions
            # aller Projekte (Badge + Hintergrund-Anzeige im Frontend).
            "agentSessions": agent_sessions_info(),
            # SHORTFORM-MODUS (01.09.): Der fruehere SF-Fork (cockpit_sf) ist
            # in den Haupt-Cockpit gewandert. Liegen SF-Artefakte im Workdir,
            # aktiviert das Frontend Caption-Spur, Reel-Split-Preview und die
            # rebuild_sf-Render-Weiche — Longform-Projekte bleiben unberuehrt.
            "sfMode": (os.path.exists(os.path.join(CFG["workdir"], "sf_layout.json"))
                       or os.path.exists(os.path.join(CFG["workdir"], "captions_sf.json"))),
            "captions": load_json(os.path.join(CFG["workdir"], "captions_sf.json"), None),
            # First-Frame-Varianten (03.09.): Bilder in <workdir>/firstframe/,
            # der Nutzer waehlt eine per Klick auf dem Hook-Clip.
            "firstFrames": self.first_frame_kandidaten(),
            "captionsMtime": (os.path.getmtime(os.path.join(CFG["workdir"], "captions_sf.json"))
                              if os.path.exists(os.path.join(CFG["workdir"], "captions_sf.json")) else None),
            "brollSync": bs_info,
            "brollUrl": ((mb + "/media/broll.mp4") if bs_info else None),
            # Selbstdiagnose fuers Frontend-Banner: was fehlt, wenn etwas fehlt
            "health": {
                "tools": tool_health(),
                "brollSyncFile": os.path.exists(os.path.join(CFG["workdir"], "broll_sync.json")),
                "brollSyncError": _BROLL_SYNC.get("error"),
                "srcVideo": bool(CFG.get("src_video") and os.path.exists(CFG.get("src_video") or "")),
            },
            "paths": {
                "workdir": CFG["workdir"],
                "overrides": CFG["overrides_path"],
                "segments": CFG["segments_path"],
                "qa": CFG["qa_path"],
            },
        }
        self._send_json(state)

    def serve_joint_audio(self, qs):
        """WAV mit +-2s um Naht k: letzte 2s von Segment k + erste 2s von Segment k+1.

        Beruecksichtigt aktuelle Nudges. Gestitcht aus audio48k.wav per numpy.
        """
        try:
            k = int(qs.get("k", ["-1"])[0])
        except ValueError:
            return self._send_json({"error": "bad k"}, HTTPStatus.BAD_REQUEST)
        window = 2.0
        try:
            window = float(qs.get("window", ["2.0"])[0])
        except ValueError:
            pass

        seg_doc = load_json(CFG["segments_path"], {})
        segments = seg_doc.get("segments", []) if isinstance(seg_doc, dict) else []
        if k < 0 or k >= len(segments) - 1:
            return self._send_json({"error": "k out of range"}, HTTPStatus.BAD_REQUEST)

        ovr = read_overrides()
        data, sr = get_audio()
        total = len(data)

        # Segment k: out-Kante (inkl. Nudge). Fenster = letzte `window` s davor.
        _, out_k = seg_edges(k, segments, ovr)
        # Segment k+1: in-Kante (inkl. Nudge). Fenster = erste `window` s danach.
        in_k1, _ = seg_edges(k + 1, segments, ovr)

        pre_start = max(0, int(round((out_k - window) * sr)))
        pre_end = min(total, int(round(out_k * sr)))
        post_start = max(0, int(round(in_k1 * sr)))
        post_end = min(total, int(round((in_k1 + window) * sr)))

        pre = data[pre_start:pre_end] if pre_end > pre_start else np.zeros(0, dtype="float32")
        post = data[post_start:post_end] if post_end > post_start else np.zeros(0, dtype="float32")

        # Kurze Stille als hoerbare Naht-Markierung zwischen den beiden Haelften
        gap = np.zeros(int(0.05 * sr), dtype="float32")
        stitched = np.concatenate([pre, gap, post]).astype("float32")

        import io
        buf = io.BytesIO()
        sf.write(buf, stitched, sr, format="WAV", subtype="PCM_16")
        wav_bytes = buf.getvalue()
        self._send_bytes(
            wav_bytes,
            "audio/wav",
            extra_headers={"Cache-Control": "no-store"},
        )

    def serve_peaks(self):
        """Waveform-Peaks der Proxy-Timeline (gecacht)."""
        data = compute_peaks()
        self._send_json(data)

    def serve_media(self, path):
        """Video-Datei mit HTTP-Range-Support (fuer Browser-Seeking noetig)."""
        # PLAYBACK-PROXY (14.08.): Fuer die WIEDERGABE wird — wenn vorhanden —
        # die All-Intra-Fassung (edit_intra_*.mp4, GOP=1) ausgeliefert. Ein
        # Seek kostet dort 1 Frame-Decode statt einer GOP-Aufholstrecke auf
        # der Long-GOP-Quelldatei; diese Aufholstrecke (250-3000 ms) war die
        # gemeinsame Wurzel der 7 Naht-Bugs (Sim-A/B 14.08.: Trim-Replay bei
        # Seek 400 ms = 28-32 Kaltspruenge, bei Seek 40 ms = 0).
        # Render und alle Datei-Operationen nutzen weiterhin die Originale.
        mapped = CFG.get("playback_map") or {}
        if path:
            p_abs = os.path.abspath(path)
            if p_abs in mapped and os.path.exists(mapped[p_abs]):
                path = mapped[p_abs]
        if not path or not os.path.exists(path):
            return self._send_json({"error": "media not found"}, HTTPStatus.NOT_FOUND)
        file_size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        ctype = "audio/wav" if path.lower().endswith(".wav") else "video/mp4"

        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not m:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", "bytes */%d" % file_size)
                self.end_headers()
                return
            start_s, end_s = m.group(1), m.group(2)
            if start_s == "":
                # suffix range: letzte N bytes
                length = int(end_s)
                start = max(0, file_size - length)
                end = file_size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", "bytes */%d" % file_size)
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            # CACHE-FIX 30.08.2026: Ohne no-store cachte Electron/Chrome alte
            # Video-Bytes ueber einen Dateitausch hinweg (V6-Intra: Sebastian
            # hoerte tagelang die ALTE, versetzte Fassung trotz Fix + Reload).
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._stream_file(path, start, length)
        else:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._stream_file(path, 0, file_size)

    def _stream_file(self, path, start, length):
        chunk = 256 * 1024
        remaining = length
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    buf = f.read(min(chunk, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass

    MEDIEN_MAX = 400 * 1024 * 1024      # 400 MB je Datei
    MEDIEN_TYPEN = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".gif": "image/gif",
                    ".mp4": "video/mp4", ".mov": "video/quicktime",
                    ".m4v": "video/mp4", ".webm": "video/webm"}

    def medien_upload(self):
        """Eigene Bilder/Videos ins Projekt holen (03.09.).

        der Nutzer will eigenes Material einsetzen koennen, etwa ein selbst
        gebautes First-Frame-Bild. Die Datei landet in <workdir>/medien/ und
        wird von dort ausgeliefert; die Timeline bekommt nur den Pfad.
        """
        name = os.path.basename(self.headers.get("X-Dateiname", "") or "")
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in self.MEDIEN_TYPEN:
            return self._send_json(
                {"error": "Dateityp nicht unterstuetzt: %r (erlaubt: %s)"
                          % (name, ", ".join(sorted(self.MEDIEN_TYPEN)))},
                HTTPStatus.BAD_REQUEST)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return self._send_json({"error": "leere Datei"}, HTTPStatus.BAD_REQUEST)
        if length > self.MEDIEN_MAX:
            return self._send_json(
                {"error": "zu gross: %.0f MB (max %d MB)"
                          % (length / 1048576.0, self.MEDIEN_MAX // 1048576)},
                HTTPStatus.BAD_REQUEST)
        d = os.path.join(CFG["workdir"], "medien")
        os.makedirs(d, exist_ok=True)
        stamm, endung = os.path.splitext(name)
        ziel = os.path.join(d, name)
        n = 2
        while os.path.exists(ziel):          # nie ueberschreiben
            ziel = os.path.join(d, f"{stamm}-{n}{endung}")
            n += 1
        tmp = ziel + ".part"
        rest = length
        with open(tmp, "wb") as f:
            while rest > 0:
                block = self.rfile.read(min(1024 * 1024, rest))
                if not block:
                    break
                f.write(block)
                rest -= len(block)
        if rest > 0:
            os.remove(tmp)
            return self._send_json({"error": "Upload abgebrochen"},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)
        os.replace(tmp, ziel)
        print("[cockpit] Medien importiert: %s (%.1f MB)"
              % (os.path.basename(ziel), length / 1048576.0), flush=True)
        return self._send_json({"ok": True, "name": os.path.basename(ziel),
                                "file": ziel, "url": "/medien/" + os.path.basename(ziel),
                                "bytes": length})

    def serve_nach_typ(self, p):
        """Video ueber serve_media (Range noetig), Bilder direkt.

        Slots koennen seit 03.09. auch Standbilder sein (importierte Medien).
        serve_media wuerde sie als video/mp4 ausliefern — der Browser zeigt
        dann nichts an.
        """
        if not p or not os.path.exists(p):
            return self._send_json({"error": "nicht gefunden"}, HTTPStatus.NOT_FOUND)
        ext = os.path.splitext(p)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            return self.serve_media(p)
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.MEDIEN_TYPEN.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def serve_medien(self, route):
        name = os.path.basename(route[len("/medien/"):])
        return self.serve_nach_typ(os.path.join(CFG["workdir"], "medien", name))

    def first_frame_kandidaten(self):
        """Bilder in <workdir>/firstframe/ = Auswahl fuer den Visual Hook."""
        d = os.path.join(CFG["workdir"], "firstframe")
        if not os.path.isdir(d):
            return []
        out = []
        aktiv = os.path.abspath(
            ((load_json(os.path.join(CFG["workdir"], "sf_layout.json"), {})
              .get("hook") or {}).get("hf") or {}).get("file") or "")
        for f in sorted(os.listdir(d)):
            if f.startswith(".") or os.path.splitext(f)[1].lower() not in (
                    ".png", ".jpg", ".jpeg", ".webp"):
                continue
            p = os.path.join(d, f)
            out.append({"name": f, "url": "/firstframe/" + f,
                        "aktiv": os.path.abspath(p) == aktiv,
                        "size": os.path.getsize(p)})
        return out

    def serve_first_frame(self, route):
        name = os.path.basename(route[len("/firstframe/"):])
        p = os.path.join(CFG["workdir"], "firstframe", name)
        if not os.path.exists(p):
            return self._send_json({"error": "nicht gefunden"}, HTTPStatus.NOT_FOUND)
        typ = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "webp": "image/webp"}.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def set_first_frame(self):
        """der Nutzer waehlt eine Variante -> sf_layout.hook.hf.file.

        Das Bild liegt als Overlay ueber dem Hook-Clip (so baut rebuild_sf es
        schon). Die Auswahl aendert nur den Dateipfad, nie die Geometrie.
        """
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            return self._send_json({"error": "bad json: %s" % e}, HTTPStatus.BAD_REQUEST)
        name = os.path.basename(str(req.get("name") or ""))
        p = os.path.join(CFG["workdir"], "firstframe", name)
        if not name or not os.path.exists(p):
            return self._send_json({"error": "Bild nicht gefunden: %r" % name},
                                   HTTPStatus.BAD_REQUEST)
        lay_p = os.path.join(CFG["workdir"], "sf_layout.json")
        lay = load_json(lay_p, {})
        lay.setdefault("hook", {}).setdefault("hf", {})["file"] = os.path.abspath(p)
        tmp = lay_p + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lay, f, ensure_ascii=False, indent=1)
        os.replace(tmp, lay_p)
        print("[cockpit] First Frame gesetzt: %s" % name, flush=True)
        return self._send_json({"ok": True, "kandidaten": self.first_frame_kandidaten()})

    def broll_starten(self, nur_status=False):
        """Ein Knopf, zwei Zustaende (03.09.).

        Sind noch keine Beats gebaut, geht der Auftrag an die Claude-Session
        im CLAUDE-Tab. Sind sie da, meldet der Endpoint sie zurueck und das
        Frontend haengt sie als Slots ein. Es entstehen NIE Slots auf Dateien,
        die es noch nicht gibt — die sahen im Cockpit aus wie fertiges B-Roll
        und liessen sich nicht abspielen (der Nutzer, 03.09.).
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "broll_auftrag.py"),
             CFG["workdir"], "--json"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return self._send_json({"error": (r.stdout + r.stderr)[-500:]},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            doc = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            return self._send_json({"error": "Auftrag unlesbar: %r" % e},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)

        fertig, offen = [], []
        for a in doc.get("auftraege", []):
            a["abs"] = os.path.join(CFG["workdir"], a["out"])
            if os.path.exists(a["abs"]) and os.path.getsize(a["abs"]) > 10000:
                fertig.append(a)
            else:
                offen.append(a)

        if not offen:
            print("[cockpit] B-Roll: %d Beats liegen vor, werden eingehaengt"
                  % len(fertig), flush=True)
            return self._send_json({"ok": True, "zustand": "fertig",
                                    "beats": fertig, "offen": 0})

        if nur_status:
            # reines Nachsehen waehrend der Agent baut: nichts beauftragen
            return self._send_json({"ok": True, "zustand": "laeuft",
                                    "beats": fertig, "offen": len(offen)})

        # Den Brief als Datei ablegen und dem Terminal nur einen KURZEN Befehl
        # schicken. Ein langer Text wird von Claude Code als Paste gebuendelt
        # ("[Pasted text #1]") und das angehaengte Return geht darin unter —
        # der Auftrag stand dann im Eingabefeld, ohne je zu starten (03.09.).
        brief = subprocess.run(
            [sys.executable, os.path.join(HERE, "broll_brief.py"),
             CFG["workdir"], "--offen-nur"], capture_output=True, text=True, timeout=60)
        text = brief.stdout.strip() if brief.returncode == 0 else ""
        if not text:
            return self._send_json({"error": "Auftragsbrief leer"},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)
        brief_p = os.path.join(CFG["workdir"], "broll_brief.txt")
        with open(brief_p, "w", encoding="utf-8") as f:
            f.write(text + "\n")

        # DIREKT an den broll-ersteller (04.09.). Vorher lief der Auftrag ueber
        # das Terminal der tmux-Session: dort sitzt eine Claude-Instanz, die den
        # Auftrag erst liest und DANN den broll-ersteller beauftragt. Dieses
        # Zwischenglied traegt nichts bei, hat den Auftrag als "[Pasted text]"
        # verschluckt und laedt ausserdem alle MCP-Server (gemessen 268s statt
        # 49s Startup, siehe reference_claude_p_headless).
        # DIREKT an den broll-ersteller. Der Umweg ueber die Claude-Session
        # im CLAUDE-Tab waere falsch: dort sitzt der video-cutter, nicht der
        # B-Roll-Agent.
        # Warum es am 04.09. zweimal "haengen blieb": nicht der Aufruf war
        # schuld (isoliert getestet, antwortet sofort), sondern ein ALTER
        # Server-Prozess, der weiterlief und noch --permission-mode acceptEdits
        # mitgab — damit darf der Agent nur Dateien schreiben, aber kein Bash,
        # und wartet ewig auf eine Freigabe. Deshalb hier KEIN permission-mode:
        # der broll-ersteller bringt seinen eigenen mit (auto).
        log_p = os.path.join(CFG["workdir"], "broll_lauf.log")
        gesendet = False
        try:
            with open(log_p, "w", encoding="utf-8") as log:
                subprocess.Popen(
                    ["claude", "-p",
                     "--agent", "broll-ersteller",
                     "--settings", '{"disableAllHooks": true}',
                     "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}'],
                    stdin=open(brief_p, "rb"), stdout=log, stderr=subprocess.STDOUT,
                    cwd=os.path.dirname(HERE), start_new_session=True)
            gesendet = True
        except Exception as e:
            print("[cockpit] broll-ersteller-Start fehlgeschlagen: %r" % e, flush=True)
        print("[cockpit] B-Roll-Auftrag: %d offen, %d fertig, ans Terminal: %s"
              % (len(offen), len(fertig), gesendet), flush=True)
        return self._send_json({"ok": True, "zustand": "beauftragt",
                                "offen": len(offen), "beats": fertig,
                                "gesendet": gesendet, "brief": text})

    def broll_auftrag(self):
        """B-Roll-Auftrag aus dem FINALEN Schnitt (03.09.).

        der Ablauf: schneiden, von Hand nachkorrigieren, dann diesen
        Knopf. Erst der Klick erklaert die Clips fuer final — ab da steht
        fest, wie viele Clips es gibt und wie lang jeder ist. Ein B-Roll pro
        Body-Clip; Hook (First-Frame-Image) und CTA (nur Text) bleiben frei.
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "broll_auftrag.py"),
             CFG["workdir"], "--json"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return self._send_json({"error": (r.stdout + r.stderr)[-500:]},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            doc = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            return self._send_json({"error": "broll_auftrag Ausgabe unlesbar: %r" % e},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)
        for a in doc.get("auftraege", []):
            a["abs"] = os.path.join(CFG["workdir"], a["out"])
            a["fertig"] = os.path.exists(a["abs"])
        # Auftragsbrief fuer den broll-ersteller gleich mitliefern. Der Knopf
        # legt ihn nur ins Prompt-Feld — 12 HyperFrames-Laeufe startet niemand
        # ungefragt (Doktrin kein_ungefragtes_rendern).
        brief = subprocess.run(
            [sys.executable, os.path.join(HERE, "broll_brief.py"),
             CFG["workdir"], "--offen-nur"], capture_output=True, text=True, timeout=60)
        doc["brief"] = brief.stdout.strip() if brief.returncode == 0 else ""
        print("[cockpit] B-Roll-Auftrag: %d Auftraege aus %d Clips"
              % (doc.get("auftrag_count", 0), doc.get("clip_count", 0)), flush=True)
        return self._send_json({"ok": True, "auftrag": doc})

    def captions_sync(self):
        # CapCut-Sync (01.09.): Captions deterministisch aus words_aai + dem
        # AKTUELLEN Schnitt neu bauen (captions_from_words.py — Seiten an
        # gemessenen Pausen, absolute Wort-Zeiten). Loest auch die Drift nach
        # Schnitt-Aenderungen. Bestehende Datei wird vom Script gesichert.
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "captions_from_words.py"),
             CFG["workdir"]], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return self._send_json({"error": (r.stdout + r.stderr)[-500:]},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)
        caps = load_json(os.path.join(CFG["workdir"], "captions_sf.json"), [])
        print("[cockpit] Caption-Sync: %d Seiten" % len(caps), flush=True)
        return self._send_json({"ok": True, "captions": caps,
                                "log": r.stdout[-300:]})

    def save_captions(self):
        # SF-Modus (aus dem cockpit_sf-Fork): Text-Clips der Caption-Spur
        # atomar nach captions_sf.json. rebuild_sf.py liest sie beim Render.
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"[]"
        try:
            incoming = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            return self._send_json({"error": "bad json: %s" % e}, HTTPStatus.BAD_REQUEST)
        if not isinstance(incoming, list):
            return self._send_json({"error": "captions muss eine Liste sein"}, HTTPStatus.BAD_REQUEST)
        path = os.path.join(CFG["workdir"], "captions_sf.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(incoming, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return self._send_json({"ok": True, "n": len(incoming),
                                "mtime": os.path.getmtime(path)})

    def save_overrides(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            incoming = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            return self._send_json({"error": "bad json: %s" % e}, HTTPStatus.BAD_REQUEST)
        # Normalisieren + Defaults
        ovr = {
            "timeline_clips": incoming.get("timeline_clips", []) or [],
            "nudges": incoming.get("nudges", {}) or {},
            "gains": incoming.get("gains", {}) or {},
            "broll": incoming.get("broll", []) or [],
            "broll_sync_off": incoming.get("broll_sync_off", []) or [],
            "broll_sync_holes": incoming.get("broll_sync_holes", []) or [],
            "broll_sync_converted": incoming.get("broll_sync_converted", []) or [],
            "extra_cut_word_ids": incoming.get("extra_cut_word_ids", []) or [],
            "uncut_word_ids": incoming.get("uncut_word_ids", []) or [],
            "deleted_segments": incoming.get("deleted_segments", []) or [],
            "splits": incoming.get("splits", []) or [],
        }
        # STALE-TAB-SCHUTZ (27.07.): Ein Tab, der vor einer externen Aenderung
        # geladen wurde, darf den neueren Stand NICHT stillschweigend
        # ueberschreiben. Bei Konflikt landet der eingehende Stand in einer
        # Sidecar-Datei (nichts geht je verloren) und der Client wird gewarnt.
        base_mtime = incoming.get("base_mtime")
        try:
            cur_mtime = os.path.getmtime(CFG["overrides_path"])
        except OSError:
            cur_mtime = None
        if (base_mtime is not None and cur_mtime is not None
                and abs(float(base_mtime) - cur_mtime) > 1.0):
            side = CFG["overrides_path"].replace(".json", ".CONFLICT-%d.json" % int(cur_mtime))
            with open(side, "w") as f:
                json.dump(ovr, f, ensure_ascii=False, indent=1)
            print("[cockpit] KONFLIKT: veralteter Tab -> %s" % side, flush=True)
            return self._send_json({"ok": False, "conflict": True, "saved_to": side,
                                    "message": "Datei wurde extern geaendert — Tab neu laden"},
                                   HTTPStatus.CONFLICT)
        write_overrides_atomic(ovr)
        seg_doc = load_json(CFG["segments_path"], {})
        segments = seg_doc.get("segments", []) if isinstance(seg_doc, dict) else []
        joint_times = compute_joint_times(segments, ovr)
        self._send_json({"ok": True, "saved": CFG["overrides_path"], "jointTimes": joint_times})

    def rerender(self):
        """Echter Render-Hook: startet rerender.py als Background-Job.

        Kette in rerender.py: effektive Decisions (extra/uncut) -> solver_v5
        -> Nudges/Gains via first_id-Mapping -> Proxy-Render -> B-Roll-Pass.
        Status via GET /api/render_status.
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if not CFG.get("src_video") or not os.path.exists(CFG["src_video"]):
            return self._send_json(
                {"error": "src_video fehlt — Server mit 6. Argument <quellvideo> starten"},
                HTTPStatus.BAD_REQUEST)
        with JOB_LOCK:
            job = CFG.get("job")
            if job and job["proc"].poll() is None:
                return self._send_json({"error": "Render läuft bereits"}, HTTPStatus.CONFLICT)
            if job and job.get("logf"):
                try:
                    job["logf"].close()
                except OSError:
                    pass
            py = sys.executable
            log_path = os.path.join(CFG["workdir"], "rerender.log")
            logf = open(log_path, "w", encoding="utf-8")
            # SF-WEICHE (01.09., aus dem cockpit_sf-Fork uebernommen): Liegt ein
            # sf_layout.json im Workdir, ist das ein Shortform-Projekt — dann
            # rendert rebuild_sf.py (Schnitt -> Karten -> Caption-Karaoke ->
            # 1080x1920-Composite) statt der Longform-Kette.
            if os.path.exists(os.path.join(CFG["workdir"], "sf_layout.json")):
                cmd = [py, os.path.join(HERE, "rebuild_sf.py"),
                       CFG["workdir"], CFG["src_video"]]
            else:
                cmd = [py, os.path.join(HERE, "rerender.py"), CFG["workdir"], CFG["src_video"],
                       CFG["words_path"], CFG["decisions_path"], CFG["proxy_path"],
                       "--mode", "proxy", "--nudge-base", CFG["segments_path"]]
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
            CFG["job"] = {"proc": proc, "log": log_path, "logf": logf}
        sys.stderr.write("[cockpit] RERENDER gestartet (pid %d)\n" % proc.pid)
        self._send_json({"status": "started", "pid": proc.pid})

    def render_status(self):
        with JOB_LOCK:
            job = CFG.get("job")
        if not job:
            return self._send_json({"status": "idle"})
        code = job["proc"].poll()
        tail = ""
        try:
            with open(job["log"], encoding="utf-8") as f:
                tail = "".join(f.readlines()[-6:])
        except OSError:
            pass
        if code is None:
            return self._send_json({"status": "running", "log": tail})
        if job.get("logf"):
            try:
                job["logf"].close()
            except OSError:
                pass
            job["logf"] = None
        self._send_json({"status": "done" if code == 0 else "failed",
                         "exit": code, "log": tail})


def main():
    _h = tool_health()
    _fehlt = [t for t, p_ in _h.items() if not p_]
    if _fehlt:
        print("[cockpit] ⚠ WERKZEUG FEHLT IM PATH: %s — B-Roll-Sync und Render "
              "funktionieren nicht!" % ", ".join(_fehlt), flush=True)
    else:
        print("[cockpit] Werkzeuge ok: %s" % ", ".join(
            "%s=%s" % (k, v) for k, v in _h.items()), flush=True)
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    workdir = os.path.abspath(sys.argv[1])
    segments_path = os.path.abspath(sys.argv[2])
    qa_path = os.path.abspath(sys.argv[3])
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 8766
    src_video = os.path.abspath(sys.argv[5]) if len(sys.argv) > 5 else None

    def first_existing(*cands):
        for c in cands:
            if os.path.exists(c):
                return c
        return cands[0]

    CFG["workdir"] = workdir
    CFG["segments_path"] = segments_path
    CFG["qa_path"] = qa_path
    CFG["overrides_path"] = os.path.join(workdir, "cockpit_overrides.json")
    CFG["proxy_path"] = first_existing(
        os.path.join(workdir, "bench", "v5_proxy.mp4"),
        os.path.join(workdir, "proxy.mp4"))
    CFG["audio_path"] = os.path.join(workdir, "audio48k.wav")
    CFG["words_path"] = first_existing(
        os.path.join(workdir, "aai", "words.json"),
        os.path.join(workdir, "words_aai.json"))
    CFG["decisions_path"] = first_existing(
        os.path.join(workdir, "aai", "decisions.json"),
        os.path.join(workdir, "decisions.json"))
    CFG["src_video"] = src_video
    CFG["port"] = port

    # PLAYBACK-PROXY (14.08.): All-Intra-Fassungen (edit_intra_*.mp4) im
    # Workdir werden fuer die WIEDERGABE ausgeliefert (GOP=1 -> Seek kostet
    # 1 Frame-Decode statt einer Aufholstrecke von 250-3000 ms auf der
    # Long-GOP-Quelle). Original bleibt Quelle fuer Render + alle Messungen.
    # Bauen: ffmpeg -i quelle -c:v libx264 -g 1 -bf 0 -crf 23 -preset veryfast
    #        -tune fastdecode -vsync cfr -c:a copy -movflags +faststart out.mp4
    playback_map = {}
    edit_main = os.path.join(workdir, "edit_intra_main.mp4")
    if src_video and os.path.exists(edit_main):
        playback_map[os.path.abspath(src_video)] = edit_main
    _bs = load_json(os.path.join(workdir, "broll_sync.json"), {}) or {}
    _bs_sources = _bs.get("sources") or ([_bs] if _bs.get("file") else [])
    for _i, _q in enumerate(_bs_sources):
        _f = _q.get("file")
        if not _f:
            continue
        _cand = os.path.join(
            workdir,
            "edit_intra_broll.mp4" if _i == 0 else "edit_intra_broll%d.mp4" % (_i + 1))
        if os.path.exists(_cand):
            playback_map[os.path.abspath(_f)] = _cand
    CFG["playback_map"] = playback_map
    for _a, _b in playback_map.items():
        print("[cockpit] Playback-Proxy aktiv: %s -> %s"
              % (os.path.basename(_a), os.path.basename(_b)), flush=True)
    if src_video and os.path.exists(src_video) and not playback_map:
        print("[cockpit] ⚠ Kein Playback-Proxy (edit_intra_main.mp4 fehlt) — "
              "Wiedergabe seekt auf der Long-GOP-Quelle (Naht-Ruckler moeglich).",
              flush=True)

    # WAV-TONSPUREN-GUARD (30.08.): Die L-Cut-Tonspuren sollen die PCM-WAV
    # spielen statt der GB-grossen mp4 (2 sinnlose Video-Decodes + Buffer-
    # Starvation, gemessen 2.7% Medienzeit-Verlust + 90-160ms Naht-Loecher).
    # Das geht NUR, wenn die WAV auf der Praesentationsachse liegt (migrierte
    # Projekte: wav_dauer == audio_start + audio_dauer der Quelle). Unmigrierte
    # Projekte (z.B. mod4v2, audio_start 0.137, WAV ab 0) bekaemen sonst
    # einen 137ms-Achsenfehler -> die bleiben auf dem mp4-Pfad.
    CFG["audio_aligned"] = False
    try:
        if os.path.exists(CFG["audio_path"]) and src_video and os.path.exists(src_video):
            _wav_dur = float(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", CFG["audio_path"]], text=True).strip())
            _a_start, _a_dur = None, None
            for _line in subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_type,start_time,duration", "-of", "csv=p=0",
                     src_video], text=True).strip().splitlines():
                _p = _line.split(",")
                if len(_p) >= 3 and _p[0] == "audio":
                    try:
                        _a_start, _a_dur = float(_p[1]), float(_p[2])
                    except ValueError:
                        pass
            if _a_dur is not None:
                CFG["audio_aligned"] = abs(_wav_dur - (_a_start + _a_dur)) < 0.10
                print("[cockpit] Tonspur-Quelle: %s (wav %.3fs vs audio %.3f+%.3fs)"
                      % ("audio48k.wav (praesentations-aligniert)" if CFG["audio_aligned"]
                         else "source.mp4 (WAV nicht aligniert — Achsen-Guard)",
                         _wav_dur, _a_start, _a_dur), flush=True)
    except Exception as _e:  # noqa: BLE001
        print("[cockpit] Tonspur-Alignment-Check fehlgeschlagen: %r" % (_e,), flush=True)

    for label, p in [
        ("workdir", workdir),
        ("segments", segments_path),
        ("qa", qa_path),
        ("proxy", CFG["proxy_path"]),
        ("audio48k", CFG["audio_path"]),
        ("words", CFG["words_path"]),
    ]:
        exists = "OK" if os.path.exists(p) else "FEHLT"
        print("[cockpit] %-10s %s  (%s)" % (label, p, exists))

    # SIGTERM (z.B. "PROJEKT WECHSELN" im Agentic OS killt per lsof) muss die
    # Claude-PTY-Session mitbeenden — sonst bleibt claude als Orphan zurueck.
    def _on_sigterm(signum, frame):  # noqa: ARG001
        term_kill()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    # allow_reuse_address: nach "PROJEKT WECHSELN" (Agentic OS killt den alten
    # Prozess per lsof) haengt der Port kurz im TIME_WAIT — ohne SO_REUSEADDR
    # scheitert der neue Bind mit "Address already in use" und der CUTTER-Tab
    # zeigt nur "cockpit.log pruefen". Mit Reuse bindet der Neustart sofort.
    ThreadingHTTPServer.allow_reuse_address = True
    # MEDIA-LISTENER auf eigenem Port (14.08.): Chromium deckelt Verbindungen
    # pro (Schema,Host,Port) auf 6. Haupt-Port traegt 4 Video-Elemente +
    # B-Roll-Layer + permanenten Terminal-SSE-Stream = Budget voll; jeder
    # Naht-Seek wartete in der Socket-Queue. Eigener Port = eigene Gruppe.
    media_port = port + 2
    try:
        media_server = ThreadingHTTPServer(("127.0.0.1", media_port), Handler)
        media_server.daemon_threads = True
        threading.Thread(target=media_server.serve_forever,
                         name="media-listener", daemon=True).start()
        CFG["media_port"] = media_port
        print("[cockpit] Media-Listener: http://127.0.0.1:%d/ (eigene Socket-Gruppe)"
              % media_port, flush=True)
    except OSError as e:
        CFG["media_port"] = None
        print("[cockpit] ⚠ Media-Port %d belegt (%s) — Media laeuft ueber den "
              "Haupt-Port (6-Socket-Limit teilt sich mit SSE/API)." % (media_port, e),
              flush=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        # Port noch echt belegt (anderer laufender Cockpit-Prozess) — klare
        # Ansage statt Traceback, damit der Launcher-Fehler verstaendlich ist.
        print("[cockpit] FEHLER: Port %d belegt (%s). Laeuft schon ein Cockpit? "
              "Alten Prozess beenden: lsof -ti :%d | xargs kill" % (port, e, port))
        sys.exit(3)
    server.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    print("[cockpit] laeuft auf %s  (Strg+C zum Beenden)" % url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        job = CFG.get("job")
        if job and job["proc"].poll() is None:
            print("\n[cockpit] beende laufenden Render-Job (pid %d) …" % job["proc"].pid)
            job["proc"].terminate()
            try:
                job["proc"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                job["proc"].kill()
        if job and job.get("logf"):
            try:
                job["logf"].close()
            except OSError:
                pass
        term_kill()
        print("\n[cockpit] beendet.")
        server.shutdown()


if __name__ == "__main__":
    main()
