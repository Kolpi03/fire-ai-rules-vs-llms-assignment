"""Record the silent demo video with Playwright (real browser, real program output).

    python scripts/demo/record_demo.py --python "$(which python)"   # interpreter that has the project installed

Requires: pip install playwright && python -m playwright install chromium; ffmpeg on PATH.
Output: evidence/demo/assignment_demo.mp4 (H.264, no audio track). The script fails if any step's exit code is
not the expected one, so a recording of a broken run is never produced silently.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "evidence/demo/assignment_demo.mp4"
VIEWPORT = {"width": 1440, "height": 900}
# step index -> (expected exit code, [(text to scroll to, seconds to hold), ...])
PLAN = {
    0: (0, [("OLD REGIME", 5), ("NEW REGIME", 5), ("RECOMMENDATION", 4)]),
    1: (0, [("OLD REGIME", 5), ("NEW REGIME", 5), ("RECOMMENDATION", 4)]),
    2: (0, [("Section 80D", 7), ("RECOMMENDATION", 3)]),
    3: (0, [("Section 80D", 6), ("RECOMMENDATION", 3)]),
    4: (0, [("Section 80D", 7), ("RECOMMENDATION", 3)]),
    5: (2, [("", 8)]),  # ask exits 2 when it needs clarification (by design)
    6: (2, [("", 7)]),
    7: (0, [("", 6)]),
    8: (0, [("", 6)]),
    9: (0, [("", 6)]),
    10: (0, [("", 12)]),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default="python3", help="interpreter used to run the project's commands")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    server = subprocess.Popen([a.python, str(ROOT / "scripts/demo/demo_server.py"), "--port", str(a.port)], cwd=ROOT)
    url = f"http://127.0.0.1:{a.port}/"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "steps", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        tmp = Path(tempfile.mkdtemp(prefix="demo_video_"))
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport=VIEWPORT, record_video_dir=str(tmp), record_video_size=VIEWPORT)
            page = ctx.new_page()
            page.goto(url)
            page.wait_for_function("STEPS.length > 0")
            page.wait_for_timeout(3000)
            for i, (expected, holds) in PLAN.items():
                code = page.evaluate(f"runStep({i})")
                if code != expected:
                    raise SystemExit(f"step {i + 1}: exit code {code}, expected {expected}")
                page.wait_for_timeout(600)
                for text, secs in holds:
                    if text:
                        page.evaluate("t => scrollTermTo(t)", text)
                    page.wait_for_timeout(int(secs * 1000))
            page.wait_for_timeout(2000)
            video = Path(page.video.path())
            ctx.close()
            browser.close()
        OUT.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-an", "-c:v", "libx264",
                        "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(OUT)],
                       check=True)
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"wrote {OUT.relative_to(ROOT)}")
        return 0
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
