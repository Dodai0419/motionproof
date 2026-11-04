#!/usr/bin/env python3
"""motionproof: prove an animation copy moves like the original.

Compare two recordings (video, gif or a folder of PNG frames) frame by frame: pixel mismatch,
activity curve, timing error and best-fit easing. Python standard library plus ffmpeg.

  python motionproof.py compare REF CLONE --out DIR [--fps 25] [--width 480] [--region x,y,w,h]
  python motionproof.py ease --series '[[0,0],[100,12],[200,40],...]'
  python motionproof.py ease --video clip.mp4 [--region x,y,w,h]
  python motionproof.py --demo
"""
import argparse, base64, html, json, math, os, re, shutil, struct, subprocess, sys, tempfile, zlib
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"

# ---- frames ----

def load_frames(src, fps, region=None, size=None, width=None, blur=0, start=None, end=None):
    """Decode a video, gif or PNG folder into a list of RGB byte strings. Returns (frames, w, h).
    shortcut: alpha is dropped (rgb24), so transparent PNGs compare by raw colour; flatten them first."""
    src = Path(src)
    listing = None
    if src.is_dir():
        pngs = sorted(p for p in src.iterdir() if p.suffix.lower() == ".png")
        if not pngs:
            sys.exit(f"no PNG frames in {src}")
        listing = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        quoted = lambda p: p.resolve().as_posix().replace("'", "'\\''")  # concat-list quoting
        listing.write("".join(f"file '{quoted(p)}'\nduration 1\n" for p in pngs))
        listing.close()
        inp, vf = ["-f", "concat", "-safe", "0", "-i", listing.name], []
    elif src.exists():
        inp, vf = ["-i", str(src)], [f"fps={fps}"]
        if start is not None:
            inp = ["-ss", str(start)] + inp
        if end is not None:
            inp += ["-t", str(end - (start or 0))]
    else:
        sys.exit(f"no such file: {src}")
    try:
        if size:
            vf.append(f"scale={size[0]}:{size[1]}:flags=lanczos")
        if region:
            vf.append("crop={2}:{3}:{0}:{1}".format(*region))
        if width and not size:
            vf.append(f"scale={width}:-2")
        probe = [FFMPEG, "-hide_banner", *inp, *(["-vf", ",".join(vf)] if vf else []), "-frames:v", "1", "-f", "null", "-"]
        log = subprocess.run(probe, capture_output=True, text=True, errors="replace").stderr
        sizes = re.findall(r"Video:.*?(\d{2,5})x(\d{2,5})", log)  # the last match is the output stream, after filters
        if not sizes:
            sys.exit("ffmpeg could not read the input:\n" + log[-400:])
        w, h = int(sizes[-1][0]), int(sizes[-1][1])
        if blur:
            vf.append(f"boxblur={blur}:2")
        cmd = [FFMPEG, "-v", "error", *inp, *(["-vf", ",".join(vf)] if vf else []), "-fps_mode", "passthrough",
               "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        raw = subprocess.run(cmd, capture_output=True)
        if raw.returncode:
            sys.exit(raw.stderr.decode(errors="replace")[-400:])
    finally:
        if listing:
            os.unlink(listing.name)
    n = w * h * 3
    return [raw.stdout[i:i + n] for i in range(0, len(raw.stdout) - n + 1, n)], w, h

def png_bytes(rgb, w, h):
    rows = b"".join(b"\0" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))

# ---- pixel distance: OKLab HyAB plus anti-aliasing detection, the rules of pixelmatch 8 for opaque pixels ----

_LIN = [(c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4) for c in range(256)]
_K1, _K2 = 0.206, 0.03
_K3 = (1 + _K1) / (1 + _K2)
_lab = {}

def _oklab(r, g, b):
    key = (r << 16) | (g << 8) | b
    v = _lab.get(key)
    if v is None:
        lr, lg, lb = _LIN[r], _LIN[g], _LIN[b]
        l = (0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb) ** (1 / 3)
        m = (0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb) ** (1 / 3)
        s = (0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb) ** (1 / 3)
        L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
        x = _K3 * L - _K1
        v = _lab[key] = (l, m, s, 0.5 * (x + math.sqrt(x * x + 4 * _K2 * _K3 * L)))
    return v

def color_differs(a, b, p, limit):
    l1, m1, s1, L1 = _oklab(a[p], a[p + 1], a[p + 2])
    l2, m2, s2, L2 = _oklab(b[p], b[p + 1], b[p + 2])
    rest = limit - abs(L1 - L2)
    if rest < 0:
        return True
    dl, dm, ds = l1 - l2, m1 - m2, s1 - s2
    da = 1.9779984951 * dl - 2.4285922050 * dm + 0.4505937099 * ds
    db = 0.0259040371 * dl + 0.7827717662 * dm - 0.8086757660 * ds
    return da * da + db * db > rest * rest

def _many_siblings(img, x1, y1, w, h):
    p = (y1 * w + x1) * 3
    val = img[p:p + 3]
    zeroes = 1 if x1 in (0, w - 1) or y1 in (0, h - 1) else 0
    for x in range(max(x1 - 1, 0), min(x1 + 1, w - 1) + 1):
        for y in range(max(y1 - 1, 0), min(y1 + 1, h - 1) + 1):
            if (x, y) != (x1, y1):
                q = (y * w + x) * 3
                if img[q:q + 3] == val:
                    zeroes += 1
                    if zeroes > 2:
                        return True
    return False

def antialiased(img, a, b, x1, y1, w, h):
    p = (y1 * w + x1) * 3
    r0, g0, b0 = img[p], img[p + 1], img[p + 2]
    zeroes = 1 if x1 in (0, w - 1) or y1 in (0, h - 1) else 0
    lo = hi = 0
    lo_xy = hi_xy = (0, 0)
    for x in range(max(x1 - 1, 0), min(x1 + 1, w - 1) + 1):
        for y in range(max(y1 - 1, 0), min(y1 + 1, h - 1) + 1):
            if (x, y) == (x1, y1):
                continue
            q = (y * w + x) * 3
            d = (r0 - img[q]) * 0.29889531 + (g0 - img[q + 1]) * 0.58662247 + (b0 - img[q + 2]) * 0.11448223
            if d == 0:
                zeroes += 1
                if zeroes > 2:
                    return False
            elif d < lo:
                lo, lo_xy = d, (x, y)
            elif d > hi:
                hi, hi_xy = d, (x, y)
    if lo == 0 or hi == 0:
        return False
    return (_many_siblings(a, *lo_xy, w, h) and _many_siblings(b, *lo_xy, w, h)) or \
           (_many_siblings(a, *hi_xy, w, h) and _many_siblings(b, *hi_xy, w, h))

_FADE = bytes(255 + (v - 255) // 10 for v in range(256))

_pairs, _rows = {}, {}

def mismatch(a, b, w, h, limit=0.1, diff=None):
    """Fraction of pixels that really differ. Plain Python stays fast because identical frames and rows are skipped
    and a row is only worked out once per distinct 5-row neighbourhood (anti-aliasing looks two rows away at most)."""
    if a == b:
        return 0.0
    if diff is None and (a, b, limit) in _pairs:
        return _pairs[a, b, limit]
    if len(_rows) > 4000:  # bounds memory on long, noisy clips, where entries rarely repeat anyway
        _rows.clear()
    count, stride = 0, w * 3
    for y in range(h):
        s = y * stride
        if a[s:s + stride] == b[s:s + stride]:
            continue
        lo, hi = max(0, y - 2) * stride, (min(h, y + 3)) * stride
        key = (w, limit, y if y < 2 or y >= h - 2 else -1, a[lo:hi], b[lo:hi])
        if diff is None and key in _rows:
            count += _rows[key]
            continue
        row = 0
        for x in range(w):
            p = s + x * 3
            if a[p] == b[p] and a[p + 1] == b[p + 1] and a[p + 2] == b[p + 2]:
                continue
            if color_differs(a, b, p, limit):
                if antialiased(a, a, b, x, y, w, h) or antialiased(b, a, b, x, y, w, h):
                    if diff is not None:
                        diff[p:p + 3] = b"\xff\xff\x00"
                else:
                    row += 1
                    if diff is not None:
                        diff[p:p + 3] = b"\xff\x00\x00"
        _rows[key] = row
        count += row
    if diff is None:
        if len(_pairs) > 4000:
            _pairs.clear()
        _pairs[a, b, limit] = count / (w * h)
    return count / (w * h)

# ---- timing ----

def motion_window(act, fps, floor=0.002):
    idx = [i for i, v in enumerate(act) if v > floor]
    first, last = (idx[0], idx[-1]) if idx else (-1, -1)
    return {"first": first, "last": last, "peak": act.index(max(act)), "durationS": 0 if first < 0 else (last - first + 1) / fps}

def best_offset(refs, clones, w, h, limit, max_shift=15):
    """The clone frame offset that best matches the first ten reference frames."""
    probe, best, offset = min(10, len(refs)), math.inf, 0
    for k in range(-max_shift, max_shift + 1):
        vals = [mismatch(refs[i], clones[i + k], w, h, limit) for i in range(probe) if 0 <= i + k < len(clones)]
        if len(vals) >= min(5, probe) and sum(vals) / len(vals) < best:
            best, offset = sum(vals) / len(vals), k
    return offset

# ---- easing ----

def _bez(x1, y1, x2, y2):
    def f(t):
        lo, hi, u = 0.0, 1.0, t
        for _ in range(30):
            u = (lo + hi) / 2
            if 3 * (1 - u) ** 2 * u * x1 + 3 * (1 - u) * u ** 2 * x2 + u ** 3 < t:
                lo = u
            else:
                hi = u
        return 3 * (1 - u) ** 2 * u * y1 + 3 * (1 - u) * u ** 2 * y2 + u ** 3
    return f

EASINGS = {
    "linear": lambda t: t, "ease": _bez(.25, .1, .25, 1), "ease-in": _bez(.42, 0, 1, 1), "ease-out": _bez(0, 0, .58, 1),
    "ease-in-out": _bez(.42, 0, .58, 1), "cubic-bezier(0.4,0,0.2,1) material standard": _bez(.4, 0, .2, 1),
    "cubic-bezier(0.2,0,0,1) material emphasized": _bez(.2, 0, 0, 1), "cubic-bezier(0.16,1,0.3,1) easeOutExpo": _bez(.16, 1, .3, 1),
    "cubic-bezier(0.22,1,0.36,1) easeOutQuint": _bez(.22, 1, .36, 1), "cubic-bezier(0.33,1,0.68,1) easeOutCubic": _bez(.33, 1, .68, 1),
    "cubic-bezier(0.25,1,0.5,1) easeOutQuart": _bez(.25, 1, .5, 1), "cubic-bezier(0.5,1,0.89,1) easeOutQuad": _bez(.5, 1, .89, 1),
    "cubic-bezier(0.65,0,0.35,1) easeInOutCubic": _bez(.65, 0, .35, 1), "cubic-bezier(0.83,0,0.17,1) easeInOutQuint": _bez(.83, 0, .17, 1),
    "cubic-bezier(0.87,0,0.13,1) easeInOutExpo": _bez(.87, 0, .13, 1), "cubic-bezier(0.76,0,0.24,1) easeInOutQuart": _bez(.76, 0, .24, 1),
    "cubic-bezier(0.45,0,0.55,1) easeInOutQuad": _bez(.45, 0, .55, 1), "cubic-bezier(0.32,0,0.67,0) easeInCubic": _bez(.32, 0, .67, 0),
    "cubic-bezier(0.7,0,0.84,0) easeInExpo": _bez(.7, 0, .84, 0), "cubic-bezier(0.34,1.56,0.64,1) easeOutBack": _bez(.34, 1.56, .64, 1),
    "cubic-bezier(0.68,-0.6,0.32,1.6) easeInOutBack": _bez(.68, -.6, .32, 1.6), "cubic-bezier(0.19,1,0.22,1) 'expo' (common in landing pages)": _bez(.19, 1, .22, 1),
    "cubic-bezier(0.77,0,0.175,1) 'power3.inOut' (gsap)": _bez(.77, 0, .175, 1), "cubic-bezier(0.075,0.82,0.165,1) easeOutCirc": _bez(.075, .82, .165, 1),
    "cubic-bezier(0.23,1,0.32,1) ease-out-quint variant": _bez(.23, 1, .32, 1),
}

def easing_family(name):
    """The families reanimate's verify.md compares: out, in, in-out, linear, spring."""
    n = name.lower().replace("-", "")
    if "back" in n:
        return "spring"
    if n == "linear":
        return "linear"
    if "inout" in n or "standard" in n:
        return "in-out"
    return "in" if "easein" in n else "out"

def fit_easing(series):
    """Fit [[t_ms, value], ...] to named easings: delay, duration, overshoot and the five best fits by RMSE."""
    if len(series) < 4:
        return {"verdict": "not enough samples", "family": None, "bestFits": []}
    start, end = series[0][1], series[-1][1]
    span = end - start
    eps = max(abs(span) * 0.01, 0.002)
    first = next((i for i, (_, v) in enumerate(series) if abs(v - start) > eps), -1)
    last = len(series) - 1
    for i in range(len(series) - 1, 0, -1):
        if abs(series[i][1] - end) > eps:
            last = min(len(series) - 1, i + 1)
            break
    first = max(first, 1)
    delay, dur = series[first - 1][0] - series[0][0], series[last][0] - series[first - 1][0]
    sign = (span > 0) - (span < 0)
    over = 0 if span == 0 else max(0, (max((v - start) * sign for _, v in series) - abs(span)) / abs(span))
    fits = []
    if span and dur > 0:
        base = series[first - 1][0]
        raw = [(t - base, (v - start) / span) for t, v in series[first - 1:]]
        for name, fn in EASINGS.items():
            best = (math.inf, dur)
            for j in range(21):  # declared duration is 1x to 2x the visible one: long tails fall under the change floor
                d = dur * (1 + j * 0.05)
                pts = [(t, y) for t, y in raw if t <= d * 1.05]
                err = math.sqrt(sum((fn(min(1, max(0, t / d))) - y) ** 2 for t, y in pts) / len(pts))
                if err < best[0]:
                    best = (err, round(d))
            fits.append({"name": name, "rmse": round(best[0], 4), "durationMs": best[1]})
        fits.sort(key=lambda f: f["rmse"])
    if over > 0.02:
        verdict = "spring-like / back easing (overshoots)"
    elif fits:
        q = "good fit" if fits[0]["rmse"] < 0.03 else "approximate" if fits[0]["rmse"] < 0.08 else "poor fit: maybe multi-step, spring, or scroll-linked"
        verdict = f"{fits[0]['name']} over ~{fits[0]['durationMs']}ms ({q})"
    else:
        verdict = "no motion"
    return {"samples": len(series), "start": round(start, 3), "end": round(end, 3), "delayMs": round(delay),
            "visibleDurationMs": round(dur), "overshootPct": round(over * 100, 1), "verdict": verdict,
            "family": "spring" if over > 0.02 else easing_family(fits[0]["name"]) if fits else None, "bestFits": fits[:5]}

def motion_series(frames, w, h, fps):
    """Turn plain frames into a progress curve, no DOM needed: the centroid of what changed since frame 0,
    or the amount of change when the centroid stays put (a fade).
    shortcut: one moving subject; use --region to isolate it when several things move."""
    first, stride, cx, cy, energy = frames[0], w * 3, [], [], []
    for f in frames:
        total = sx = sy = 0
        if f != first:
            for y in range(h):
                s = y * stride
                if f[s:s + stride] == first[s:s + stride]:
                    continue
                for x in range(w):
                    p = s + x * 3
                    d = abs(f[p] - first[p]) + abs(f[p + 1] - first[p + 1]) + abs(f[p + 2] - first[p + 2])
                    if d > 12:  # video noise floor
                        total += d
                        sx += d * x
                        sy += d * y
        cx.append(sx / total if total else None)
        cy.append(sy / total if total else None)
        energy.append(total)
    known = lambda c: [v for v in c if v is not None]
    travel = lambda c: max(known(c)) - min(known(c)) if known(c) else 0
    if max(travel(cx), travel(cy)) < 2:
        vals = energy
    else:
        c = cx if travel(cx) >= travel(cy) else cy
        # before anything moves there is no centroid: hold the rest position, half a step before the first moved value
        vals, last = [], 2 * known(c)[0] - known(c)[1] if len(known(c)) > 1 else known(c)[0]
        for v in c:
            last = last if v is None else v
            vals.append(last)
    return [[round(i * 1000 / fps, 2), v] for i, v in enumerate(vals)]

# ---- compare ----

def compare(ref, clone, out, fps=25, region=None, width=None, limit=0.1, threshold=0.05, motion_limit=0.08,
            blur=0, offset=None, start=None, end=None):
    out = Path(out)
    (out / "diff").mkdir(parents=True, exist_ok=True)
    refs, w, h = load_frames(ref, fps, region, width=width, start=start, end=end)
    clones, cw, ch = load_frames(clone, fps, region, width=width, start=start, end=end)
    if (cw, ch) != (w, h):
        print(f"clone frames are {cw}x{ch}, reference {w}x{h}: rescaling clone", file=sys.stderr)
        clones, _, _ = load_frames(clone, fps, region, size=(w, h), start=start, end=end)
    if not refs or not clones:
        sys.exit("no frames found")
    rc, cc = refs, clones
    if blur:
        rc, _, _ = load_frames(ref, fps, region, width=width, blur=blur, start=start, end=end)
        cc, _, _ = load_frames(clone, fps, region, size=(w, h), blur=blur, start=start, end=end)
    act = lambda fs: [0.0] + [mismatch(fs[i - 1], fs[i], w, h, limit) for i in range(1, len(fs))]
    ref_act, clone_act = act(refs), act(clones)
    if offset is None:
        offset = best_offset(rc, cc, w, h, limit)
    rows = []
    for i in range(len(rc)):
        j = i + offset
        ok = 0 <= j < len(cc)
        row = {"i": i, "t": round(i / fps, 3), "cloneIndex": j if ok else None,
               "mismatch": mismatch(rc[i], cc[j], w, h, limit) if ok else None}
        if ok and blur:
            row["sharpMismatch"] = mismatch(refs[i], clones[j], w, h, limit)
        rows.append(row)
    done = [r for r in rows if r["mismatch"] is not None]
    mean = sum(round(r["mismatch"], 4) for r in done) / max(1, len(done))
    worst = sorted(done, key=lambda r: -r["mismatch"])[:5]
    rw, cw_ = motion_window(ref_act, fps), motion_window(clone_act[max(0, offset):], fps)
    start_delay = round((motion_window(clone_act, fps)["first"] - rw["first"]) / fps, 3)  # before alignment, so a late copy cannot hide
    easing = {"reference": fit_easing(motion_series(refs, w, h, fps)), "clone": fit_easing(motion_series(clones, w, h, fps))}
    err = {"startS": round((cw_["first"] - rw["first"]) / fps, 3), "endS": round((cw_["last"] - rw["last"]) / fps, 3),
           "peakS": round((cw_["peak"] - rw["peak"]) / fps, 3), "durationS": round(cw_["durationS"] - rw["durationS"], 3)}
    max_err = max(abs(err["startS"]), abs(err["endS"]), abs(err["durationS"]))
    report = {"fps": fps, "size": [w, h], "region": region, "offset": offset, "threshold": threshold, "motionThreshold": motion_limit,
              "pixelThreshold": limit, "frames": len(rows), "compared": len(done), "meanMismatch": round(mean, 4),
              "maxMismatch": round(worst[0]["mismatch"], 4) if worst else 0, "worstFrames": worst,
              "motion": {"reference": rw, "clone": cw_, "timingError": err, "startDelayS": start_delay},
              "easing": easing,
              "activity": {"reference": [round(v, 4) for v in ref_act], "clone": [round(v, 4) for v in clone_act]},
              "pass": {"pixels": mean <= threshold, "timing": max_err <= motion_limit, "start": abs(start_delay) <= motion_limit,
                       "easing": easing["reference"]["family"] == easing["clone"]["family"]}, "frameDetails": rows}
    report["pass"]["motion"] = report["pass"]["timing"] and report["pass"]["start"] and report["pass"]["easing"]
    if blur:
        report["blur"] = blur
        report["sharpMeanMismatch"] = round(sum(r["sharpMismatch"] for r in done) / max(1, len(done)), 4)
    even = done[:: max(1, len(done) // 8)][:8]
    picks = sorted({r["i"]: r for r in even + worst[:2]}.values(), key=lambda r: r["i"])
    shots = {}
    for r in picks:
        diff = bytearray(refs[r["i"]].translate(_FADE))
        mismatch(refs[r["i"]], clones[r["cloneIndex"]], w, h, limit, diff)
        shots[r["i"]] = png_bytes(bytes(diff), w, h)
        (out / "diff" / f"{r['i']:04d}.png").write_bytes(shots[r["i"]])
    (out / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    md = _markdown(report, ref, clone)
    (out / "report.md").write_text(md + "\n", encoding="utf-8")
    (out / "report.html").write_text(_html(report, refs, clones, shots, picks, w, h, ref, clone), encoding="utf-8")
    print(md)
    return report

def _markdown(r, ref, clone):
    m, e, p = r["motion"], r["motion"]["timingError"], r["pass"]
    bar = lambda v: "#" * min(40, round(v * 400)) or "."
    lines = [f"# compare: {ref} vs {clone}", "",
             f"- frames compared: {r['compared']}/{r['frames']} at {r['fps']} fps, clone offset {r['offset']}",
             f"- mean mismatch: **{r['meanMismatch'] * 100:.2f}%** (threshold {r['threshold'] * 100:.1f}%) -> {'PASS' if p['pixels'] else 'FAIL'}"
             + (f"; sharp: {r['sharpMeanMismatch'] * 100:.2f}%" if "blur" in r else ""),
             f"- motion window: reference frames {m['reference']['first']}-{m['reference']['last']} ({m['reference']['durationS']:.2f}s, peak #{m['reference']['peak']}); "
             f"clone {m['clone']['first']}-{m['clone']['last']} ({m['clone']['durationS']:.2f}s, peak #{m['clone']['peak']})",
             f"- start delay before alignment (clone - ref): {m['startDelayS']}s -> {'PASS' if p['start'] else 'FAIL'}",
             f"- timing error after alignment (clone - ref): start {e['startS']}s, end {e['endS']}s, duration {e['durationS']}s, peak {e['peakS']}s -> {'PASS' if p['timing'] else 'FAIL'} (limit {r['motionThreshold']}s)",
             f"- easing: reference {r['easing']['reference']['verdict']}; clone {r['easing']['clone']['verdict']}; "
             f"family {r['easing']['reference']['family']} vs {r['easing']['clone']['family']} -> {'PASS' if p['easing'] else 'FAIL'}",
             f"- moves the same: {'PASS' if p['motion'] else 'FAIL'}",
             "- worst frames: " + ", ".join(f"#{w['i']} ({w['mismatch'] * 100:.1f}% @ {w['t']}s)" for w in r["worstFrames"]),
             "", "## activity per frame (how much changed since the previous frame)", "", "```",
             "frame   t(s)   reference                                 clone"]
    a = r["activity"]
    for f in r["frameDetails"]:
        c = "" if f["cloneIndex"] is None else bar(a["clone"][f["cloneIndex"]])
        lines.append(f"{f['i']:5d} {f['t']:6.2f}   {bar(a['reference'][f['i']]):<40}  {c}")
    return "\n".join(lines + ["```"])

def _html(r, refs, clones, shots, picks, w, h, ref, clone):
    uri = lambda png: "data:image/png;base64," + base64.b64encode(png).decode()
    a, e = r["activity"], r["motion"]["timingError"]
    top = max(max(a["reference"]), max(a["clone"]), 1e-6)
    def curve(vals, color):
        n = max(len(vals) - 1, 1)
        pts = " ".join(f"{i / n * 800:.1f},{120 - v / top * 112:.1f}" for i, v in enumerate(vals))
        return f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>'
    badge = lambda ok: f'<b class="{"ok" if ok else "bad"}">{"PASS" if ok else "FAIL"}</b>'
    rows = "".join(f'<tr><td>#{p["i"]}<br>{p["t"]:.2f}s<br>{p["mismatch"] * 100:.1f}%</td><td><img alt="" src="{uri(png_bytes(refs[p["i"]], w, h))}"></td>'
                   f'<td><img alt="" src="{uri(png_bytes(clones[p["cloneIndex"]], w, h))}"></td><td><img alt="" src="{uri(shots[p["i"]])}"></td></tr>' for p in picks)
    return f"""<!doctype html><meta charset="utf-8"><title>motionproof report</title><style>
body{{font:14px/1.45 ui-sans-serif,system-ui,sans-serif;margin:24px auto;max-width:860px;padding:0 16px;color:#1b1f24;background:#fff}}
h1{{font-size:20px}}.ok{{color:#0a7d33}}.bad{{color:#c4261d}}table{{border-collapse:collapse;width:100%}}td,th{{padding:4px;text-align:left;font-size:12px;vertical-align:top}}
img{{width:100%;display:block;outline:1px solid #d5d9de}}svg{{width:100%;background:#f5f6f8}}.k i{{display:inline-block;width:12px;height:3px;margin:0 4px 3px 12px}}</style>
<h1>{html.escape(Path(str(ref)).name)} vs {html.escape(Path(str(clone)).name)}</h1>
<p>Pixels: {r['meanMismatch'] * 100:.2f}% mean mismatch (limit {r['threshold'] * 100:.1f}%) {badge(r['pass']['pixels'])}<br>
Moves the same: {badge(r['pass']['motion'])}<br>
Start delay before alignment: {r['motion']['startDelayS']}s {badge(r['pass']['start'])}<br>
Timing after alignment: start {e['startS']}s, end {e['endS']}s, duration {e['durationS']}s (limit {r['motionThreshold']}s) {badge(r['pass']['timing'])}<br>
Easing family: {r['easing']['reference']['family']} vs {r['easing']['clone']['family']} {badge(r['pass']['easing'])}<br>
Reference easing: {html.escape(r['easing']['reference']['verdict'])}<br>Clone easing: {html.escape(r['easing']['clone']['verdict'])}</p>
<p class="k">Activity per frame<i style="background:#2563eb"></i>reference<i style="background:#e8590c"></i>clone</p>
<svg viewBox="0 0 800 124" role="img" aria-label="Activity curves of reference and clone">{curve(a['reference'], '#2563eb')}{curve(a['clone'], '#e8590c')}</svg>
<table><tr><th style="width:64px">frame</th><th>reference</th><th>clone</th><th>diff</th></tr>{rows}</table>"""

# ---- demo ----

def scene(t_ms, ease, x0=40, x1=360, delay=0, dur=600, color=(37, 99, 235), w=480, h=200):
    x = x0 + (x1 - x0) * ease(min(1, max(0, (t_ms - delay) / dur)))
    bg = bytes((245, 246, 248))
    row_bg = bg * w
    row = bytearray(row_bg)
    for px in range(max(0, int(x)), min(w, int(x) + 81)):
        cover = min(1, px + 1 - x) if px < x + 1 else min(1, x + 80 - px)
        if cover > 0:
            row[px * 3:px * 3 + 3] = bytes(round(g + (c - g) * cover) for g, c in zip(bg, color))
    return b"".join(bytes(row) if 60 <= y < 140 else row_bg for y in range(h))

def make_clip(path, ease, n=45, fps=25, **kw):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (path / f"{i:04d}.png").write_bytes(png_bytes(scene(i * 1000 / fps, ease, **kw), 480, 200))

def demo(out="motionproof-demo"):
    out = Path(out)
    ease = EASINGS["cubic-bezier(0.33,1,0.68,1) easeOutCubic"]
    make_clip(out / "reference", ease, delay=200)
    make_clip(out / "clone-round1", EASINGS["linear"], delay=320, dur=900)
    make_clip(out / "clone-round2", ease, delay=200, color=(232, 89, 12))
    print("Round 1: a first attempt, linear easing, 120 ms late, 300 ms too long\n")
    r1 = compare(out / "reference", out / "clone-round1", out / "round1")
    print("\nRound 2: same easing and timing, own colour\n")
    r2 = compare(out / "reference", out / "clone-round2", out / "round2")
    verdict = lambda r: "PASS" if r["pass"]["motion"] else "FAIL"
    print(f"\nMoves the same: round 1 {verdict(r1)}, round 2 {verdict(r2)}. Reports: {out / 'round1' / 'report.html'}, {out / 'round2' / 'report.html'}")

# ---- cli ----

def _rect(s):
    return tuple(int(v) for v in s.split(",")) if s else None

def main(argv=None):
    if argv is None and "--demo" in sys.argv[1:]:
        return demo()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare", help="compare two recordings")
    c.add_argument("ref")
    c.add_argument("clone")
    c.add_argument("--out", required=True)
    c.add_argument("--fps", type=float, default=25)
    c.add_argument("--region")
    c.add_argument("--width", type=int)
    c.add_argument("--start", type=float)
    c.add_argument("--end", type=float)
    c.add_argument("--pixel-threshold", type=float, default=0.1)
    c.add_argument("--threshold", type=float, default=0.05)
    c.add_argument("--motion-threshold", type=float, default=0.08)
    c.add_argument("--blur", type=int, default=0)
    c.add_argument("--offset", type=int)
    e = sub.add_parser("ease", help="name the easing of a curve or a recording")
    e.add_argument("--series")
    e.add_argument("--video")
    e.add_argument("--fps", type=float, default=25)
    e.add_argument("--region")
    e.add_argument("--width", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "compare":
        compare(a.ref, a.clone, a.out, a.fps, _rect(a.region), a.width, a.pixel_threshold, a.threshold, a.motion_threshold, a.blur, a.offset, a.start, a.end)
    elif a.series or a.video:
        if a.series:
            series = json.loads(a.series)
        else:
            frames, w, h = load_frames(a.video, a.fps, _rect(a.region), width=a.width)
            series = motion_series(frames, w, h, a.fps)
        print(json.dumps(fit_easing(series), indent=2))
    else:
        sys.exit("pass --series or --video")

if __name__ == "__main__":
    main()
