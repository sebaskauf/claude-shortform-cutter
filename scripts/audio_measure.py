#!/usr/bin/env python3
"""V5 Kern-Modul: sample-nahe Audio-Vermessung (RMS, Otsu-Schwellwert, Pausen,
Sprech-Offsets/-Onsets). Visuell verifiziert am 12.07.2026 (bench_gt Worst-Case-
PNGs). Wird von solver.py und qa_loop.py importiert. Reines numpy, kein Torch.

Konventionen:
- "Pause" = zusammenhaengende Stille >= min_pause_s nach Otsu-Schwellwert,
  Blips < 30ms (Mundklicks) unterbrechen keine Stille.
- offset  = Ende der letzten Sprachenergie VOR einer Pause (Wort-Ende, echt)
- onset   = Beginn der ersten Sprachenergie NACH einer Pause (Wort-Start, echt)
"""
import numpy as np
import soundfile as sf

CLICK_S = 0.030
RUN_MIN_S = 0.015   # Sprach-Run muss >=15ms sein um als Sprache zu zaehlen

def load_mono(path):
    x, sr = sf.read(path, dtype="float32")   # float32: halbiert Memory (71min = 0,8 statt 1,6GB)
    if x.ndim > 1: x = x.mean(axis=1)
    return x, sr

def rms_db(x, sr, win_ms=10.0, hop_ms=2.5):
    """Blockweise RMS: Fenster wird auf Hop-Granularitaet gerastert (win=4*hop
    default). Vermeidet den 1,6GB-cumsum ueber alle Samples bei langen Videos
    (OOM-Kill-Fix 13.07.) — Genauigkeit fuer unsere Zwecke identisch."""
    hop = max(1, int(sr * hop_ms / 1000))
    win_blocks = max(1, round(win_ms / hop_ms))
    n_blocks = len(x) // hop
    if n_blocks < win_blocks + 1:
        return np.array([0.0]), np.array([-100.0]), hop / sr
    x2 = x[:n_blocks * hop].astype(np.float32)
    block_sums = (x2 * x2).reshape(n_blocks, hop).sum(axis=1, dtype=np.float64)
    cs = np.concatenate([[0.0], np.cumsum(block_sums)])
    n = n_blocks - win_blocks
    win_sums = cs[win_blocks:win_blocks + n] - cs[:n]
    rms = np.sqrt(win_sums / (win_blocks * hop))
    t = np.arange(n) * hop / sr
    return t, 20 * np.log10(rms + 1e-10), hop / sr

def otsu_db(db):
    h, edges = np.histogram(db, bins=120, range=(-90, -10))
    h = h.astype(np.float64); total = h.sum()
    mids = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(h); w1 = total - w0
    m0 = np.cumsum(h * mids) / np.maximum(w0, 1e-9)
    m1 = (np.sum(h * mids) - np.cumsum(h * mids)) / np.maximum(w1, 1e-9)
    var = (w0 / total) * (w1 / total) * (m0 - m1) ** 2
    return float(mids[int(np.argmax(var))])

def sprech_schwelle(x, sr, reserve_db=9.0):
    """Sprech-/Stille-Schwelle fuer eine ganze Spur — GEMESSEN, nicht geschaetzt.

    Der naheliegende Weg (Perzentil-Rauschteppich + 9 dB) bricht, sobald der
    Ton nicht mehr von der Kamera kommt: ein externes Ansteckmikro hat
    einen Rauschteppich um -84 dB, floor+9 landet damit bei -75 dB und liegt
    immer noch mitten im Rauschen. Der Onset-Detektor findet dann ueberall
    "Sprache", saemtliche Kanten-Clamps greifen ins Leere — und zwar STILL,
    ohne Fehlermeldung. Gefunden am 03.09.2026 im ersten Mikro-Lauf (sf0912),
    wo Otsu bei -58,3 dB trennte statt bei -74,7.

    Otsu trennt die beiden Verteilungen (Stille / Sprache) dort, wo sie
    tatsaechlich liegen, unabhaengig vom Aufnahmegeraet.

    Rueckgabe: (floor_wie_frueher, schwelle_db). Aufrufer, die bisher
    "floor + 9" gerechnet haben, koennen floor_wie_frueher einsetzen und ihre
    Rechnung unveraendert lassen.
    """
    _t, db, _hop = rms_db(np.asarray(x, dtype=np.float32), sr)
    schwelle = otsu_db(db)
    return schwelle - reserve_db, schwelle


def _close_blips(mask, hop_s, max_gap_s=CLICK_S):
    m = mask.copy(); i, n = 0, len(m)
    max_len = int(max_gap_s / hop_s)
    while i < n:
        if not m[i]:
            j = i
            while j < n and not m[j]: j += 1
            if i > 0 and j < n and (j - i) <= max_len:
                m[i:j] = True
            i = j
        else:
            i += 1
    return m

class AudioMap:
    """Einmal pro Datei bauen; liefert Pausen + verfeinerte Offsets/Onsets."""

    def __init__(self, audio_path, min_pause_s=0.15):
        self.x, self.sr = load_mono(audio_path)
        self.t, self.db, self.hop_s = rms_db(self.x, self.sr)
        self.thr = otsu_db(self.db)
        self.min_pause_s = min_pause_s
        self.pauses = self._find_pauses()

    def _find_pauses(self):
        sil = _close_blips(self.db < self.thr, self.hop_s)
        pauses, i, n = [], 0, len(sil)
        while i < n:
            if sil[i]:
                j = i
                while j < n and sil[j]: j += 1
                if i > 0 and j < n and self.t[j - 1] - self.t[i] >= self.min_pause_s:
                    ps, pe = self.t[i], self.t[j - 1]
                    depth = float(np.median(self.db[i:j]) - self.thr)
                    pauses.append({"start": ps, "end": pe, "dur": pe - ps, "depth_db": depth})
                i = j
            else:
                i += 1
        return pauses

    def _fine_runs(self, t0, t1):
        """5ms-RMS-Runs ueber Schwellwert in [t0,t1] -> Liste (run_start, run_end)."""
        a = max(0, int(t0 * self.sr)); b = min(len(self.x), int(t1 * self.sr))
        if b - a < self.sr // 100: return []
        t5, db5, hop5 = rms_db(self.x[a:b], self.sr, win_ms=5.0, hop_ms=1.0)
        above = db5 > self.thr
        run_min = max(1, int(RUN_MIN_S / hop5))
        runs, i, n = [], 0, len(above)
        while i < n:
            if above[i]:
                j = i
                while j < n and above[j]: j += 1
                if (j - i) >= run_min:
                    runs.append((a / self.sr + t5[i], a / self.sr + t5[j - 1] + 0.005))
                i = j
            else:
                i += 1
        return runs

    def speech_offset(self, pause):
        """Echtes Sprach-Ende vor der Pause (letzter Run-Endpunkt)."""
        runs = self._fine_runs(pause["start"] - 0.40, pause["start"] + 0.05)
        return runs[-1][1] if runs else pause["start"]

    def speech_onset(self, pause):
        """Echter Sprach-Beginn nach der Pause (erster Run-Startpunkt)."""
        runs = self._fine_runs(pause["end"] - 0.05, pause["end"] + 0.40)
        return runs[0][0] if runs else pause["end"]

    def pause_after(self, t, max_ahead=1.5):
        """Erste Pause deren Start in [t-0.25, t+max_ahead] liegt."""
        for p in self.pauses:
            if p["start"] >= t - 0.25 and p["start"] <= t + max_ahead:
                return p
        return None

    def pause_before(self, t, max_back=1.5):
        """Letzte Pause deren Ende in [t-max_back, t+0.25] liegt."""
        best = None
        for p in self.pauses:
            if p["end"] <= t + 0.25 and p["end"] >= t - max_back:
                best = p
        return best
