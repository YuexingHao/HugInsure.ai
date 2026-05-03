#!/usr/bin/env python3
"""Record a HugClaims product demo with Playwright.

The script records a deterministic browser walkthrough:
  1. homepage correction hover
  2. chat prompt + streamed answer + bounty panel
  3. claim page edit/diff
  4. verifier result and final claim screen

Playwright records WebM. If ffmpeg is available, the script also writes MP4 and
GIF versions next to the WebM.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:
    print(
        "Missing dependency: playwright\n"
        "Install with: pip install playwright && python -m playwright install chromium",
        file=sys.stderr,
    )
    raise


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_PROMPT = (
    "Audit this proof. Claim: every pointwise convergent sequence of continuous "
    "functions on [0,1] converges uniformly. Proof: for each x choose N_x for "
    "epsilon/3; by continuity this works in a neighborhood of x; compactness "
    "gives a finite subcover, so take the maximum N_x. Is the proof valid?"
)
FOLLOWUP_PROMPT = (
    "Now test it on f_n(x)=x^n on [0,1]. Keep the answer tight and name the "
    "exact hidden quantifier mistake."
)
CLAIM_EDIT_INSERT = (
    "Correction to file: the finite-subcover step is invalid because the N_x "
    "comes from pointwise convergence at a single point. Continuity of one tail "
    "function does not give one N that works on a whole neighborhood, so the "
    "proof quietly swaps pointwise control for uniform local control."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record a HugClaims demo video.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Local or hosted HugClaims base URL.")
    parser.add_argument("--out-dir", default=str(ROOT / "recordings"), help="Directory for output videos.")
    parser.add_argument("--name", default=f"hugclaims-demo-{time.strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt to type into the chat.")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--slow-mo", type=int, default=35, help="Playwright slow motion in ms.")
    parser.add_argument("--keep-open", action="store_true", help="Leave the browser open at the end.")
    parser.add_argument("--no-gif", action="store_true", help="Skip GIF conversion even if ffmpeg exists.")
    return parser.parse_args()


def wait(page: Page, ms: int) -> None:
    page.wait_for_timeout(ms)


def click_when_ready(page: Page, selector: str, timeout: int = 15000) -> None:
    loc = page.locator(selector)
    loc.wait_for(state="visible", timeout=timeout)
    loc.click()


def convert_with_ffmpeg(webm: Path, mp4: Path, gif: Path | None) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print(f"Full ffmpeg not found; kept WebM only: {webm}")
        return

    try:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(webm),
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "scale=1440:-2",
                str(mp4),
            ],
            check=True,
        )
        print(f"Wrote MP4: {mp4}")
    except subprocess.CalledProcessError as exc:
        print(f"MP4 conversion failed; kept WebM only: {exc}", file=sys.stderr)
        return

    if gif is None:
        return

    try:
        palette = gif.with_suffix(".palette.png")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(webm),
                "-vf",
                "fps=12,scale=1000:-1:flags=lanczos,palettegen",
                str(palette),
            ],
            check=True,
        )
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(webm),
                "-i",
                str(palette),
                "-lavfi",
                "fps=12,scale=1000:-1:flags=lanczos[x];[x][1:v]paletteuse",
                str(gif),
            ],
            check=True,
        )
        palette.unlink(missing_ok=True)
        print(f"Wrote GIF: {gif}")
    except subprocess.CalledProcessError as exc:
        print(f"GIF conversion failed; kept WebM/MP4 outputs: {exc}", file=sys.stderr)


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def wait_for_chat_turn(page: Page) -> None:
    try:
        page.wait_for_function(
            """
            () => {
              const assistants = document.querySelectorAll('#convo .msg.assistant');
              const last = assistants[assistants.length - 1];
              if (!last) return false;
              return !last.innerHTML.includes('cursor') && last.textContent.trim().length > 35;
            }
            """,
            timeout=90000,
        )
    except PlaywrightTimeoutError:
        print("Timed out waiting for chat completion; continuing with current page state.", file=sys.stderr)


def send_chat_turn(page: Page, prompt: str) -> None:
    ta = page.locator("#ta")
    ta.click()
    ta.fill(prompt)
    wait(page, 180)
    click_when_ready(page, "#send")
    wait_for_chat_turn(page)


def run_demo(page: Page, base_url: str, prompt: str) -> None:
    page.goto(f"{base_url}/index.html", wait_until="networkidle")
    wait(page, 450)

    # Homepage: show the wrong -> right correction affordance.
    page.locator(".hero .swap").first.hover()
    wait(page, 650)
    page.mouse.move(80, 80)
    wait(page, 180)
    page.locator(".examples .swap").nth(1).hover()
    wait(page, 550)

    click_when_ready(page, 'a.cta[href="chat.html"]')
    page.wait_for_url("**/chat.html")
    page.wait_for_load_state("networkidle")
    wait(page, 350)

    # Chat: use a two-round hard math proof audit.
    send_chat_turn(page, prompt)
    wait(page, 700)
    send_chat_turn(page, FOLLOWUP_PROMPT)

    # Wait until the bounty panel has updated.
    try:
        page.wait_for_function(
            """
            () => {
              const verdict = document.querySelector('#riskVerdict')?.textContent?.trim() || '';
              return verdict && verdict !== '—';
            }
            """,
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        print("Timed out waiting for score update; continuing with current page state.", file=sys.stderr)

    wait(page, 900)
    page.locator(".bet-panel").scroll_into_view_if_needed()
    wait(page, 450)

    # Claim: save the chat snapshot and move to the correction workflow.
    click_when_ready(page, "#claimBtn")
    page.wait_for_url("**/claim.html")
    page.wait_for_load_state("networkidle")
    wait(page, 450)

    assistant = page.locator("#snapshot .msg.assistant").last
    assistant.scroll_into_view_if_needed()
    wait(page, 250)
    assistant.click()
    wait(page, 250)
    page.evaluate(
        """(insert) => {
          const msg = document.querySelector('#snapshot .msg.assistant.editing');
          if (!msg) return;
          const text = msg.textContent.trim();
          const target = /hidden quantifier mistake[^.]*\\./i;
          if (target.test(text)) {
            msg.textContent = text.replace(target, (m) => `${m}\\n\\n${insert}`);
          } else {
            msg.textContent = `${text}\\n\\n${insert}`;
          }
        }""",
        CLAIM_EDIT_INSERT,
    )
    wait(page, 350)
    click_when_ready(page, ".edit-toolbar .done")
    wait(page, 650)

    click_when_ready(page, "#verifyBtn")
    try:
        page.wait_for_selector(".verdict.show", timeout=90000)
    except PlaywrightTimeoutError:
        print("Timed out waiting for verifier result; continuing.", file=sys.stderr)
    wait(page, 900)

    click_when_ready(page, "#submitBtn")
    if page.locator("#confirmModal.open #confirmYes").count():
        wait(page, 500)
        click_when_ready(page, "#confirmYes")
    wait(page, 1200)


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / f"{args.name}-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, slow_mo=args.slow_mo)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=1,
            record_video_dir=str(raw_dir),
            record_video_size={"width": args.width, "height": args.height},
        )
        page = context.new_page()
        run_demo(page, args.base_url.rstrip("/"), args.prompt)

        if args.keep_open:
            print("Keeping browser open. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        video = page.video
        context.close()
        browser.close()

        if video is None:
            raise RuntimeError("Playwright did not produce a video.")

        webm = out_dir / f"{args.name}.webm"
        Path(video.path()).replace(webm)

    mp4 = out_dir / f"{args.name}.mp4"
    gif = None if args.no_gif else out_dir / f"{args.name}.gif"
    convert_with_ffmpeg(webm, mp4, gif)
    print(f"Wrote WebM: {webm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
