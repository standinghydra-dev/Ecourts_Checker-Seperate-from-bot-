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

NOTE ON ARCHITECTURE: all cases now run in ONE shared browser session
within this single process (checker.case_checker.run_all_cases), instead
of the old model that spawned a fresh subprocess+browser per case. This
was changed because eCourts appears to rate-limit/block based on how many
distinct "fresh session" requests hit it in a short window -- a shared
session with spacing between cases avoids that far more reliably. See
checker/case_checker.py's run_all_cases() docstring for the safety nets
that replace the old per-case OS-level subprocess timeout.
"""

import os
import sys
import json
import logging
import argparse
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
ROOT = Path(__file__).parent
DELAY_BETWEEN_CASES = 8    # seconds between cases within the shared session
PER_CASE_TIMEOUT    = 200  # hard wall-clock cap per case (POSIX only)

# ── Imports (after path setup) ────────────────────────────────────────────────
sys.path.insert(0, str(ROOT))
from storage  import load_cases, update_status
from notifier import notify
from checker.case_checker import run_all_cases


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

    results, changes_from_run, _ = run_all_cases(
        cases, last_status,
        logger=logger.info,
        detailed=True,
        delay_between_cases=DELAY_BETWEEN_CASES,
        per_case_timeout=PER_CASE_TIMEOUT,
    )

    logger.info("=" * 60)
    logger.info(
        "Done. %d/%d ok.",
        sum(1 for r in results if r.get("raw_ok")), len(results)
    )

    # Persist status and get the official changes list (storage.py owns the
    # persisted "last known status" — run_all_cases' own change detection
    # above is only used for its ⚡ log lines during the run)
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
