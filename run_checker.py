"""
run_checker.py — Main entry point for ecourts-checker.

Usage:
  python run_checker.py              # check all cases, notify via all channels
  python run_checker.py --dry-run    # check but print summary, don't notify
  python run_checker.py --no-email   # skip email even if configured
  python run_checker.py --no-tg      # skip Telegram even if configured

Triggered by:
  - GitHub Actions (scheduled or manual via workflow_dispatch)
  - Locally: just run the script
"""

import os
import sys
import json
import logging
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT            = Path(__file__).parent
RUN_CASES_SCRIPT = ROOT / "checker" / "run_cases.py"
TIMEOUT_PER_CASE = 300   # 5-minute hard kill per case (same as original)

# ── Imports (after path setup) ────────────────────────────────────────────────
sys.path.insert(0, str(ROOT))
from storage  import load_cases, update_status
from notifier import notify


# ── Per-case subprocess runner (identical logic to original scraper.py) ────────

def _run_one_case(case: dict, last_status: dict) -> dict:
    """Spawn run_cases.py for a single case with a hard 5-min timeout."""
    payload = json.dumps(
        {"case": case, "last_status": last_status, "detailed": True},
        ensure_ascii=False,
    )
    cnr   = case.get("cnr", "?")
    label = case.get("label", cnr)

    proc = subprocess.Popen(
        ["timeout", "-k", "10", str(TIMEOUT_PER_CASE),
         sys.executable, str(RUN_CASES_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,   # inherit: live logs in terminal / Actions log
        text=True,
    )

    stdout_data = ""
    try:
        stdout_data, _ = proc.communicate(
            input=payload,
            timeout=TIMEOUT_PER_CASE + 60,
        )
    except subprocess.TimeoutExpired:
        logger.error("communicate() backup timeout for '%s' — force killing", label)
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    except Exception as e:
        logger.error("communicate() error for '%s': %s", label, e)

    if proc.returncode in (124, -15, -9):
        logger.error("Case '%s' killed after %ds — Chromium froze", label, TIMEOUT_PER_CASE)
        return {
            "cnr": cnr, "label": label, "raw_ok": False,
            "error": f"Killed after {TIMEOUT_PER_CASE}s — Chromium froze",
            "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    try:
        output = json.loads(stdout_data)
        return output.get("result", {
            "cnr": cnr, "label": label, "raw_ok": False,
            "error": "Empty result from subprocess",
        })
    except Exception as e:
        logger.error("Failed to parse result for '%s': %s", label, e)
        return {
            "cnr": cnr, "label": label, "raw_ok": False,
            "error": "Result parse failed",
            "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="eCourts case checker")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Check cases but print summary; don't send notifications")
    parser.add_argument("--no-email", action="store_true",
                        help="Skip email even if EMAIL_* env vars are set")
    parser.add_argument("--no-tg",   action="store_true",
                        help="Skip Telegram even if TELEGRAM_* env vars are set")
    args = parser.parse_args()

    # Determine trigger source (GitHub Actions sets GITHUB_EVENT_NAME)
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch":
        triggered_by = "manual (GitHub)"
    elif event == "schedule":
        triggered_by = "scheduled"
    else:
        triggered_by = "manual (local)"

    # Suppress channels if flags passed
    if args.no_email:
        os.environ.pop("EMAIL_SENDER",    None)
        os.environ.pop("EMAIL_PASSWORD",  None)
        os.environ.pop("EMAIL_RECIPIENT", None)
    if args.no_tg:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID",   None)

    cases = load_cases()
    if not cases:
        logger.error("No cases found in cases.json — nothing to do.")
        sys.exit(1)

    logger.info("Starting check for %d case(s) — triggered by: %s", len(cases), triggered_by)
    logger.info("=" * 60)

    # Build last_status keyed by CNR for change detection
    from storage import load_status
    old_status = load_status()
    last_status = {
        cnr: data.get("_key", "")
        for cnr, data in old_status.items()
    }

    results = []
    for i, case in enumerate(cases, 1):
        label = case.get("label", case.get("cnr", "?"))
        logger.info("[%d/%d] Checking: %s (%s)", i, len(cases), label, case.get("cnr", "?"))
        result = _run_one_case(case, last_status)
        if "label" not in result:
            result["label"] = label
        results.append(result)
        ok = "✅" if result.get("raw_ok") else "❌"
        logger.info(
            "  %s %s | Stage: %s | Next: %s",
            ok, label,
            result.get("case_status", result.get("error", "?")),
            result.get("next_hearing", "—"),
        )

    logger.info("=" * 60)
    logger.info(
        "Done. %d/%d ok.",
        sum(1 for r in results if r.get("raw_ok")), len(results)
    )

    # Persist status and get changes
    changes = update_status(results)
    if changes:
        logger.info("Changes detected: %s", ", ".join(changes))
    else:
        logger.info("No status changes.")

    if args.dry_run:
        logger.info("--dry-run: skipping notifications.")
        # Print a readable summary to stdout
        print("\n" + "=" * 60)
        print("DRY RUN SUMMARY")
        print("=" * 60)
        from notifier import build_summary
        payload = build_summary(results, changes, triggered_by)
        print(payload["email_plain"])
    else:
        tg_ok, email_ok = notify(results, changes, triggered_by)
        logger.info("Telegram: %s | Email: %s",
                    "sent" if tg_ok else "skipped/failed",
                    "sent" if email_ok else "skipped/failed")


if __name__ == "__main__":
    main()
