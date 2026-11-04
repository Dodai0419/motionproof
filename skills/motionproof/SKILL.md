---
name: motionproof
description: "Compare an animation copy against its original from two recordings (mp4, mov, webm, gif or PNG frames). Use when rebuilding or cloning an animation, checking whether motion matches a reference, or naming the easing and timing of a clip."
---

# motionproof

Grade an animation copy against its original with numbers, then fix the biggest difference and grade it again. Requires Python 3.9+ and ffmpeg on PATH. It does not film anything: get both recordings first, at the same size and frame rate.

## Commands

Run from the repo root:

```bash
python motionproof.py compare reference.mp4 copy.mp4 --out proof --width 480
python motionproof.py ease --video reference.mp4
python motionproof.py ease --series "[[0,0],[100,30],[200,60],[300,80]]"
python motionproof.py --demo
```

Options for `compare`: `--fps`, `--region x,y,w,h`, `--width`, `--start`, `--end`, `--threshold` (default 0.05, use 0.02 for a pixel-exact copy), `--motion-threshold` (default 0.08 s), `--pixel-threshold`, `--blur N` for particles and grain, `--offset K`.

## The loop

1. Run `ease --video` on the reference and use the verdict and fitted `durationMs` in the first build.
2. Film the copy the same way as the reference.
3. Run `compare`, and read `proof/report.md` and `pass` in `proof/report.json`.
4. Fix one cause, then run it again. Keep each round's folder.
5. Stop when `pass.motion` holds in `proof/report.json`: start delay, aligned timing and easing family all match. For a pixel-exact copy, `pass.pixels` must hold too. After 5 rounds with no progress, report the last numbers and the reason. Never loosen a threshold silently.

## Reading the numbers

- `pass.motion` is the verdict for motion: `pass.start`, `pass.timing` and `pass.easing` together.
- `pass.start` compares the raw start of motion before alignment. A copy that starts late fails it.
- `pass.timing` is measured after alignment, so on its own it does not catch a late start.
- `pass.easing` compares best-fit families: out, in, in-out, linear, spring.
- A copy with its own colours fails `pass.pixels` and can still pass `pass.motion`. That is correct.
- Easing from video assumes one main moving subject. Use `--region` when several things move.
- Pure Python: pass `--width 480` for large clips.
