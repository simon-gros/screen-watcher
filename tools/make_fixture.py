#!/usr/bin/env python
"""Turn a live recording into a sanitized replay fixture.

The Priority 0 acceptance gate asks for fixtures that exercise
representative states *through the same application path used by live
capture*. That rules out storing cropped regions: the replay backend
serves whole frames and the profile resolves regions out of them, so a
crop would bypass the geometry the fixture is meant to test.

Full frames are 10.9 MB each, which is not committable. Blanking every
pixel outside the regions the profile actually reads keeps the geometry
and the capture path identical while compressing to under 1 MB, because
the blanked area is a single flat colour.

Sanitizing matters beyond size: a full frame of someone's screen carries
their display name, clan chat, private messages and friends list. A
masked frame carries only the panels the rules read.

    python tools/make_fixture.py --config profiles/boss-arch-glacor.json \\
        --frames state/recording --out tests/fixtures/glacor --keep 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher                                          # noqa: E402


def enabled_regions(cfg: dict) -> set[str]:
    """Regions at least one enabled rule reads.

    Unused regions are dropped rather than blanked-and-kept: a fixture
    should not imply coverage of a detector that is switched off.
    """
    return {r["region"] for r in cfg["rules"] if r.get("enabled", True)}


def mask_frame(frame: np.ndarray, cfg: dict, names: set[str]) -> np.ndarray:
    masked = np.zeros_like(frame)
    height, width = frame.shape[:2]
    for name in names:
        region = cfg["_regions"].get(name)
        if region is None:
            continue
        x, y, w, h = region.resolve((width, height))
        masked[y:y + h, x:x + w] = frame[y:y + h, x:x + w]
    return masked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="profile to read regions from")
    ap.add_argument("--frames", required=True, help="directory of recorded frames")
    ap.add_argument("--out", required=True, help="directory to write the fixture to")
    ap.add_argument("--keep", type=int, default=3,
                    help="how many frames to keep, evenly spaced")
    ap.add_argument("--consecutive", action="store_true",
                    help="keep adjacent frames instead of spacing them out, "
                         "so the fixture can exercise scroll detection")
    ap.add_argument("--start", type=int, default=0,
                    help="index of the first frame to keep")
    args = ap.parse_args()

    cfg = watcher.load_config(Path(args.config))
    names = enabled_regions(cfg)
    if not names:
        print("profile has no enabled rules", file=sys.stderr)
        return 1

    sources = sorted(Path(args.frames).glob("*.png"))
    if not sources:
        print(f"no frames in {args.frames}", file=sys.stderr)
        return 1

    if args.consecutive:
        # Adjacent frames, because `ocr_scrolling` can only reuse cached text
        # when the chat moved less than _SCROLL_MAX (200px). Evenly spaced
        # frames from a long recording scroll far past that, so the optimiser
        # falls back to a full re-read and the fixture silently measures the
        # slow path - which is how a 3.7x saving went untested.
        chosen = sources[args.start:args.start + args.keep]
    else:
        # Evenly spaced rather than the first N, so a short fixture still spans
        # the recording's range of states instead of one moment repeated.
        step = max(1, len(sources) // max(1, args.keep))
        chosen = sources[args.start::step][:args.keep]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for i, src in enumerate(chosen):
        with Image.open(src) as im:
            frame = np.asarray(im.convert("RGB"))
        path = out / f"frame{i:05d}.png"
        Image.fromarray(mask_frame(frame, cfg, names)).save(path, optimize=True)
        total += path.stat().st_size

    print(f"wrote {len(chosen)} frames to {out} ({total / 1e6:.1f} MB)")
    print(f"regions kept: {', '.join(sorted(names))}")
    print(f"replay with: SCREEN_WATCHER_REPLAY={out} "
          f"watcher.py --config {args.config} --backend replay doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
