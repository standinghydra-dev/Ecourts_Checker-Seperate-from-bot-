"""
run_cases.py — Subprocess entry point: processes exactly ONE case.

TIMEOUT: signal.alarm() is set as the VERY FIRST thing — before any
imports — so it fires even if Playwright's import hangs under memory
pressure. Previous version set the alarm inside main() which runs after
all imports, meaning a hang during 'from case_checker import run_all_cases'
(which imports Playwright) was invisible to the alarm.

Two-layer kill:
  1. signal.alarm(280) — kernel-level, set before any imports
  2. scraper.py communicate(timeout=300) — backup OS kill from parent

Input:  JSON on stdin  → {"case": {...}, "last_status": {...}, "detailed": bool}
Output: JSON on stdout → {"result": {...}}
Logs:   stderr inherited by parent → live in Render logs
"""

# ── SIGALRM must be set BEFORE any other imports ──────────────────────────────
# Playwright's import can hang under memory pressure. If alarm is set after
# the import, a hang there is completely invisible — no signal, no output.
import signal as _signal
import sys as _sys
import json as _json

_SIGALRM_SECONDS = 280

def _timeout_handler(signum, frame):
    # Write directly to stderr before any other imports are available
    _sys.stderr.write(f"  ⏰ SIGALRM: case exceeded {_SIGALRM_SECONDS}s — killing\n")
    _sys.stderr.flush()
    raise TimeoutError(f"Case exceeded {_SIGALRM_SECONDS}s")

_signal.signal(_signal.SIGALRM, _timeout_handler)
_signal.alarm(_SIGALRM_SECONDS)

# ── Now safe to do remaining imports (protected by alarm) ─────────────────────
from pathlib import Path
from datetime import datetime

_sys.path.insert(0, str(Path(__file__).parent))
from case_checker import run_all_cases   # imports Playwright — now protected


def main():
    try:
        payload = _json.loads(_sys.stdin.read())
    except Exception as e:
        _write({"error": f"Failed to parse stdin: {e}"})
        _sys.exit(1)

    case        = payload.get("case", {})
    last_status = payload.get("last_status", {})
    detailed    = payload.get("detailed", False)
    cnr         = case.get("cnr", "?")
    label       = case.get("label", cnr)

    try:
        results, _, _ = run_all_cases(
            cases=[case],
            last_status=last_status,
            detailed=detailed,
            logger=lambda msg: print(msg, file=_sys.stderr, flush=True),
        )
        result = results[0] if results else {
            "cnr": cnr, "label": label, "raw_ok": False, "error": "No result"
        }
    except TimeoutError as e:
        result = {
            "cnr":          cnr,
            "label":        label,
            "raw_ok":       False,
            "error":        f"Timed out after {_SIGALRM_SECONDS}s (SIGALRM)",
            "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    except Exception as e:
        result = {
            "cnr":          cnr,
            "label":        label,
            "raw_ok":       False,
            "error":        str(e)[:300],
            "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    finally:
        _signal.alarm(0)  # cancel alarm on clean exit

    _write({"result": result})


def _write(obj):
    print(_json.dumps(obj, ensure_ascii=False), file=_sys.stdout, flush=True)


if __name__ == "__main__":
    main()
