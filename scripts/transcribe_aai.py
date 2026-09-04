#!/usr/bin/env python3
"""AssemblyAI-Transkription (Deutsch, verbatim) -> erfasst ALLE Wiederholungs-Takes.
Key aus env AAI_KEY. Output:
  <out>/words_aai.json  : [{id,text,start,end,conf}]  (Sekunden)
  <out>/script_aai.txt  : lesbar, mit Pausen-Markern
"""
import os, sys, json, time, requests

KEY = os.environ["AAI_KEY"]
audio = sys.argv[1]; outdir = sys.argv[2]
os.makedirs(outdir, exist_ok=True)
H = {"authorization": KEY}
BASE = "https://api.assemblyai.com/v2"

def upload(path):
    print("[aai] lade Audio hoch ...", flush=True)
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/upload", headers=H, data=f)
    r.raise_for_status(); return r.json()["upload_url"]

# universal-3-pro ist ein Speech-LLM: ohne prompt SUPPRESST es unsichere Woerter (= fehlende Woerter).
# Dieser prompt zwingt es zu best-guess + verbatim (Fueller/Stotterer behalten). Wirkt fuer Deutsch.
PROMPT = ("Required: Preserve the original language(s) and script as spoken, including code-switching "
          "and mixed-language phrases. Mandatory: Preserve linguistic speech patterns including "
          "disfluencies, filler words, hesitations, repetitions, stutters, false starts, and colloquialisms "
          "in the spoken language. Always: Transcribe speech with your best guess based on context in all "
          "possible scenarios where speech is present in the audio.")

def transcribe(url):
    body = {"audio_url": url, "language_code": "de", "speech_models": ["universal-3-pro"], "prompt": PROMPT}
    r = requests.post(f"{BASE}/transcript", headers=H, json=body)
    if r.status_code != 200:
        print(f"[aai] submit {r.status_code}: {r.text[:400]}", flush=True); return None
    tid = r.json()["id"]; print(f"[aai] Job {tid} (universal-3-pro + best-guess-prompt) ...", flush=True)
    while True:
        s = requests.get(f"{BASE}/transcript/{tid}", headers=H).json()
        if s.get("status") in ("completed", "error"): return s
        time.sleep(4)

def main():
    url = upload(audio)
    res = transcribe(url)
    if res is None or res.get("status") != "completed":
        print(f"[aai] FEHLER: {res.get('error') if res else 'kein Submit'}", flush=True); sys.exit(1)

    ws = res.get("words", []) or []
    words = [{"id": f"w{i}", "text": w["text"], "start": round(w["start"]/1000, 3),
              "end": round(w["end"]/1000, 3), "conf": round(w.get("confidence", 0), 2)}
             for i, w in enumerate(ws)]
    json.dump(words, open(os.path.join(outdir, "words_aai.json"), "w"), ensure_ascii=False, indent=0)

    # lesbares Skript: an Satzende / Pause>0.45s umbrechen, Pausen markieren
    with open(os.path.join(outdir, "script_aai.txt"), "w") as f:
        line, lstart = [], None
        for i, w in enumerate(words):
            if lstart is None: lstart = w["start"]
            line.append(w["text"])
            gap = (words[i+1]["start"] - w["end"]) if i+1 < len(words) else 999
            if w["text"].endswith((".", "!", "?")) or gap > 0.45:
                f.write(f"[{lstart:7.2f}-{w['end']:7.2f}] {' '.join(line)}\n")
                if gap > 0.8 and i+1 < len(words): f.write(f"         ...[PAUSE {gap:.2f}s]...\n")
                line, lstart = [], None
        if line: f.write(f"[{lstart:7.2f}] {' '.join(line)}\n")

    dur = words[-1]["end"] if words else 0
    print(f"[aai] FERTIG: {len(words)} Woerter, Audio-Ende {dur:.0f}s. "
          f"Sprache={res.get('language_code')}. -> words_aai.json / script_aai.txt", flush=True)

if __name__ == "__main__":
    main()
