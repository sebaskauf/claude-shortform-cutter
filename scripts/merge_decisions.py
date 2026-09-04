#!/usr/bin/env python3
"""V6: vereinigt Global-Pass-Sektionen + Fenster-Agenten-Cuts zu decisions.json.

Regeln:
- Union aller Ranges (Wort-Indizes), Gruende werden gesammelt
- Einzelwort-Cuts mit confidence < 0.8 fliegen raus, AUSSER sie grenzen an
  eine Pause > 1.5s (V1-Lesson: Einzelwort-Cuts mitten im Fluss = zerhackt)
- Cuts mit confidence < 0.5 fliegen raus
- Output: decisions.json (cut_word_ids) + decisions_report.md (fuer Gegenleser
  + Cockpit, pro zusammenhaengender Cut-Gruppe Grund/Zeit/Text)

Usage: merge_decisions.py <words.json> <global_cuts.json> <win_results.json> <outdir>
  win_results.json = Array von {"cuts":[...]} (ein Eintrag pro Fenster-Agent)
"""
import sys, json

def main():
    words_path, global_path, wins_path, outdir = sys.argv[1:5]
    words = json.load(open(words_path))
    idx = {w["id"]: i for i, w in enumerate(words)}
    n = len(words)

    ranges = []
    for s in json.load(open(global_path))["sections"]:
        ranges.append({**s, "src": "global"})
    for winres in json.load(open(wins_path)):
        for c in winres.get("cuts", []):
            ranges.append({**c, "src": "window"})

    cut = [None] * n   # None=keep, sonst Liste von Gruenden
    dropped = []
    for r in ranges:
        conf = r.get("confidence", 0.5)
        if r["from_id"] not in idx or r["to_id"] not in idx:
            dropped.append({**r, "why": "unbekannte ID"}); continue
        a, b = idx[r["from_id"]], idx[r["to_id"]]
        if a > b:
            dropped.append({**r, "why": "from>to"}); continue
        if conf < 0.5:
            dropped.append({**r, "why": "confidence<0.5"}); continue
        if a == b and conf < 0.8:
            gap_after = (words[a+1]["start"] - words[a]["end"]) if a+1 < n else 99
            gap_before = (words[a]["start"] - words[a-1]["end"]) if a > 0 else 99
            if max(gap_after, gap_before) < 1.5:
                dropped.append({**r, "why": "einzelwort mitten im fluss"}); continue
        for k in range(a, b + 1):
            if cut[k] is None: cut[k] = []
            cut[k].append(f"[{r['src']} {conf:.1f}] {r.get('reason','')[:80]}")

    cut_ids = [words[i]["id"] for i in range(n) if cut[i] is not None]
    dec = {"cut_word_ids": cut_ids, "compress_pauses_over": 3.0}
    json.dump(dec, open(f"{outdir}/decisions.json", "w"), ensure_ascii=False, indent=0)

    # Report: zusammenhaengende Cut-Gruppen
    lines = ["# V6 Schnitt-Entscheidungen (auto-generiert)", ""]
    i = 0; groups = 0
    while i < n:
        if cut[i] is None: i += 1; continue
        j = i
        while j + 1 < n and cut[j + 1] is not None: j += 1
        txt = " ".join(words[k]["text"] for k in range(i, min(j + 1, i + 14)))
        if j - i >= 14: txt += " …"
        reasons = sorted(set(sum((cut[k] for k in range(i, j + 1)), [])))[:3]
        lines.append(f"## CUT {words[i]['id']}-{words[j]['id']} "
                     f"({words[i]['start']:.1f}-{words[j]['end']:.1f}s, {j-i+1} Wörter)")
        lines.append(f"> {txt}")
        for rr in reasons: lines.append(f"- {rr}")
        lines.append("")
        groups += 1; i = j + 1
    open(f"{outdir}/decisions_report.md", "w").write("\n".join(lines))

    keep_words = n - len(cut_ids)
    cut_time = sum(words[idx[c]]["end"] - words[idx[c]]["start"] for c in cut_ids)
    print(f"[merge] {groups} Cut-Gruppen, {len(cut_ids)}/{n} Wörter geschnitten "
          f"(~{cut_time/60:.1f} min Sprechzeit), {keep_words} bleiben. "
          f"{len(dropped)} Ranges verworfen.")
    for d in dropped[:10]:
        print(f"[merge]   verworfen ({d['why']}): {d.get('from_id')}-{d.get('to_id')} {d.get('reason','')[:60]}")
    json.dump(dropped, open(f"{outdir}/dropped_ranges.json", "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
