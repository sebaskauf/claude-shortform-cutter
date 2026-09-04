#!/usr/bin/env python3
"""V6: Review-Liste generieren — alles was der Nutzer anschauen sollte, mit
Proxy-Timecodes zum Direkt-Anspringen. Quellen:
- qa_repair-Ergebnis (Graufaelle + Warnungen + Repairs)
- Gegenleser-Befunde (Feinschliff: Intra-Satz-Stotterer, Rest-Dopplungen)
- Dead-Air-Strecken >20s die komprimiert wurden
- manuelle Notizen (z.B. Content-Gaps)

Usage: generate_review.py <workdir> <segments_repaired.json> <verify_qa.json> <gegenleser.json> <out_md>
"""
import sys, os, json

def fmt(t):
    return f"{int(t//60)}:{t%60:04.1f}"

def main():
    workdir, segs_path, qa_path, gg_path, out_md = sys.argv[1:6]
    doc = json.load(open(segs_path))
    S = doc["segments"]
    QA = json.load(open(qa_path))
    gg = json.load(open(gg_path)) if os.path.exists(gg_path) else {"issues": []}
    words = json.load(open(os.path.join(workdir, "words_aai.json")))
    widx = {w["id"]: w for w in words}

    # Proxy-Zeit: kumulierte Segmentlaengen; source->proxy Mapping
    cum, seg_cum = 0.0, []
    for s in S:
        seg_cum.append(cum)
        cum += s["out"] - s["in"]

    def src_to_proxy(t):
        for k, s in enumerate(S):
            if s["in"] <= t <= s["out"]:
                return seg_cum[k] + (t - s["in"])
        best = min(range(len(S)), key=lambda k: abs(S[k]["in"] - t))
        return seg_cum[best]

    rep = doc.get("repair", {})
    repairs = rep.get("repairs", [])
    all_fails = [r for r in QA if not r["pass"]]
    # Atmer-Klasse (nur joint_not_silent, Text-Checks gruen) = Warnung, kein Graufall
    warns = [r for r in all_fails
             if {f.split("(")[0] for f in r["fails"]} == {"joint_not_silent"}]
    fails_now = [r for r in all_fails if r not in warns]

    lines = [f"# V6 Review-Liste ({len(S)} Segmente, {cum/60:.1f} min geschnittenes Video)", ""]

    # Content-Gaps aus workdir/content_gaps.json (pro Video, nicht hardcoden!)
    gaps_path = os.path.join(workdir, "content_gaps.json")
    gaps = (json.load(open(gaps_path)).get("gaps", [])
            if os.path.exists(gaps_path) else [])
    if gaps:
        lines.append("## 🔴 WICHTIG: Content-Gaps im Rohmaterial")
        for g in gaps:
            lines.append(f"- {g}")
        lines.append("")
    else:
        lines += ["## Content-Gaps", "- keine — alles Angekündigte wird im Video eingelöst ✓", ""]

    lines.append(f"## Graufälle aus der QA ({len(fails_now)} Nähte — bitte kurz reinhören)")
    for r in fails_now:
        pt = src_to_proxy(r["out"])
        kinds = ", ".join(sorted({f.split("(")[0] for f in r["fails"]}))
        lines.append(f"- **{fmt(pt)}** (Quelle {fmt(r['out'])}) — {kinds}")
        lines.append(f"  davor: \"…{r['heard_pre'][-45:]}\" | danach: \"{r['heard_post'][:45]}…\"")
    lines.append("")

    if repairs:
        lines.append(f"## Auto-Repairs ({len(repairs)} — zur Info, sollten ok sein)")
        for rp in repairs:
            pt = src_to_proxy(rp.get("new", rp.get("old", 0)))
            lines.append(f"- {fmt(pt)}: Kante {rp['edge']} {rp['old']} → {rp['new']}")
        lines.append("")

    if warns:
        lines.append(f"## Atmer-Warnungen ({len(warns)} — Text-Checks grün, vermutlich nur Atem/Decay im Nahtfenster)")
        lines.append("- " + ", ".join(fmt(src_to_proxy(w["out"])) for w in warns))
        lines.append("")

    # Gegenleser-Feinschliff (check-Items, nicht auto-appliziert)
    checks = [i for i in gg.get("issues", []) if i.get("vorschlag") == "check"
              and i.get("confidence", 0) >= 0.7]
    lines.append(f"## Feinschliff-Kandidaten vom Gegenleser ({len(checks)} — im Cockpit nachcutten wenn stört)")
    for i in checks:
        w = widx.get(i["from_id"])
        if not w: continue
        pt = src_to_proxy(w["start"])
        lines.append(f"- **{fmt(pt)}** [{i['type']}] {i['beschreibung'][:110]}")
    lines.append("")

    # Komprimierte Dead-Air-Strecken >20s
    lines.append("## Komprimierte Stille-Strecken >20s (im Cockpit zurückholbar, falls Demo wichtig war)")
    n_da = 0
    for k in range(len(S) - 1):
        gap = S[k + 1]["in"] - S[k]["out"]
        if gap > 20:
            n_da += 1
            lines.append(f"- {fmt(seg_cum[k] + (S[k]['out']-S[k]['in']))} im Proxy: "
                         f"{gap:.0f}s Original-Stille entfernt (Quelle {fmt(S[k]['out'])})")
    if n_da == 0:
        lines.append("- keine")
    lines.append("")
    lines.append(f"Gesamt: {len(S)} Segmente · {len(repairs)} Auto-Repairs · "
                 f"{len(fails_now)} Graufälle · {len(checks)} Feinschliff-Kandidaten")

    open(out_md, "w").write("\n".join(lines) + "\n")
    print(f"[review] {out_md} geschrieben: {len(fails_now)} Graufälle, "
          f"{len(checks)} Feinschliff, {n_da} Dead-Air >20s")

if __name__ == "__main__":
    main()
