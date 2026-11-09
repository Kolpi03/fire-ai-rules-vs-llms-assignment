# Silent demo video

`evidence/demo/assignment_demo.mp4` is a recording of the project's **real command-line application** running in a
browser terminal (the project has no web UI of its own, so none was invented). Every line in the video is streamed from
the running program; no audio track.

| File | Purpose |
|---|---|
| `demo_server.py` | Local-only (127.0.0.1) page that runs a fixed, allow-listed list of the project's commands and streams their output. Removes API keys/tokens from the subprocess environment, so no LLM call is made. |
| `index.html` | The browser terminal (types the command, shows the live output, highlights key lines). |
| `final_summary.py` | Final results screen: figures read from `benchmarks/*.json`, `project_a/data/*.json` and a live validator run. |
| `record_demo.py` | Playwright recording (`record_video_dir`) + FFmpeg conversion to H.264 MP4 with `-an` (no audio). Fails if any step's exit code differs from the expected one. |

Steps shown: FY 2024-25 and FY 2025-26 old vs new regime; Section 80D with an insured non-senior child vs an insured
senior spouse (approved compiled rules) and an uninsured senior's medical bills; a clarifying question from the
plain-language parser; an ambiguous input rejected; the approved compiled rules re-verified (242/242); the 30-case
official-calculator comparison; the 80D test suite; final results.

Re-record:

```bash
pip install playwright && python -m playwright install chromium    # once, in any virtualenv
python scripts/demo/record_demo.py --python "$(which python)"      # interpreter with the project requirements
```
