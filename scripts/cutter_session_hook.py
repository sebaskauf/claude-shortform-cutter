#!/usr/bin/env python3
"""Claude-Code-Hook fuer persistente Cutter-Sessions (01.09.2026).

Läuft bei SessionStart / UserPromptSubmit / Stop / SessionEnd der Claude-
Session im Cut-Cockpit (tmux, Projekt-Umgebung setzt CUTTER_WORKDIR) und
schreibt zwei kleine Dateien ins Workdir:

  agent_status.json    {status, ts, session_id, aufgabe}
                       -> Cockpit-Badge ("arbeitet"/"fertig"), auch fuer
                          Hintergrund-Sessions anderer Projekte sichtbar.
  claude_session.json  {session_id, ts}
                       -> Reboot-Recovery: tmux ueberlebt keinen Neustart,
                          der Cockpit-Server startet dann
                          `claude --agent video-cutter --resume <id>`.

WICHTIG: Der Hook ist in den PROJEKT-Settings des Video-Cutter-Verzeichnisses
registriert und feuert damit fuer JEDE Claude-Session in diesem Projekt. Ohne
CUTTER_WORKDIR (normale Sessions) beendet er sich sofort und lautlos —
er darf niemals eine fremde Session stoeren (immer Exit 0, nie stdout).

Nur Python-Stdlib — laeuft ohne venv.
"""
import json
import os
import sys
import time


def _write_atomic(path, doc):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def main():
    wd = os.environ.get("CUTTER_WORKDIR")
    if not wd or not os.path.isdir(wd):
        return                       # kein Cockpit-Kontext -> still raus
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    ev = data.get("hook_event_name") or ""
    sid = data.get("session_id")
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    if sid:
        _write_atomic(os.path.join(wd, "claude_session.json"),
                      {"session_id": sid, "ts": now})

    status, aufgabe = None, None
    if ev == "UserPromptSubmit":
        status = "arbeitet"
        aufgabe = " ".join((data.get("prompt") or "").split())[:120]
    elif ev == "Stop":
        status = "fertig"
    elif ev == "SessionStart":
        status = "bereit"
    elif ev == "SessionEnd":
        status = "beendet"
    if not status:
        return

    sp = os.path.join(wd, "agent_status.json")
    doc = {"status": status, "ts": now, "session_id": sid}
    if aufgabe:
        doc["aufgabe"] = aufgabe
    else:
        try:                          # letzte Aufgabe behalten ("fertig: <was>")
            doc["aufgabe"] = (json.load(open(sp)) or {}).get("aufgabe")
        except Exception:
            pass
    _write_atomic(sp, doc)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass                          # ein Hook darf NIE eine Session brechen
    sys.exit(0)
