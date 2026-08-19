"""
eCourts Case Checker — Core Scraping Module
============================================
Handles browser automation + CAPTCHA solving.
Returns structured data. No email/Telegram logic here.
"""

import io
import os
import re
import sys
import time
import json
import platform
from collections import Counter
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── OCR setup ─────────────────────────────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image

    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

ECOURTS_URL = "https://services.ecourts.gov.in/ecourtindia_v6/"

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str, log_path: Path = None):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

# ── CAPTCHA solving ───────────────────────────────────────────────────────────
def _preprocess(img, threshold: int, scale: int = 4):
    """
    Simple binarization — best for eCourts Securimage CAPTCHAs.

    Testing on real images showed:
    - PSM 7 (single line) reads this CAPTCHA correctly on its own
    - MedianFilter / Contrast enhance / invert all hurt accuracy
    - Plain grayscale → scale → threshold is cleanest
    """
    img = img.convert("L")
    w, h = img.size
    img = img.resize((w * scale, h * scale), Image.LANCZOS)
    img = img.point(lambda p: 0 if p < threshold else 255)
    return img


def solve_captcha(img_bytes: bytes, logger=None) -> str:
    """
    Solve eCourts Securimage CAPTCHA (always exactly 6 alphanumeric chars).

    Key findings:
    - PSM 7 is correct for this font; PSM 8/13 consistently misread digits
      (e.g. '9' → '0' or dropped). The old multi-PSM voting buried the right answer.
    - Exact 6-char length filter removes all noise results before voting.
    - Scales 3–5 all work; varying threshold gives diverse candidates to vote on.
    """
    if not OCR_AVAILABLE:
        return ""
    try:
        base = Image.open(io.BytesIO(img_bytes))
        cfg = (
            "--psm 7 --oem 3 "
            "-c tessedit_char_whitelist="
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        )
        counts = Counter()

        for scale in [3, 4, 5]:
            for threshold in [110, 120, 130, 140, 160, 180]:
                try:
                    processed = _preprocess(base.copy(), threshold, scale)
                    text = pytesseract.image_to_string(processed, config=cfg, timeout=15)
                    text = text.strip().replace(" ", "").replace("\n", "")

                    # eCourts CAPTCHA is always exactly 6 chars — hard filter
                    if len(text) == 6 and text.isalnum():
                        counts[text] += 1
                        if counts[text] >= 4:  # dominant early exit
                            if logger:
                                logger(f"  OCR candidates: {dict(counts)} → chose '{text}' (early exit)")
                            return text
                except Exception as e:
                    if logger:
                        logger(f"  OCR attempt error (scale={scale}, threshold={threshold}): {e}")
                    continue

        if not counts:
            if logger:
                logger("  OCR: no valid 6-char candidates found")
            return ""

        best, freq = counts.most_common(1)[0]
        if logger:
            logger(f"  OCR candidates: {dict(counts)} → chose '{best}' ({freq}x)")
        return best

    except Exception as e:
        if logger:
            logger(f"  OCR error: {e}")
        return ""


# ── Browser helpers ───────────────────────────────────────────────────────────
def _find_input(page, selectors: list, timeout_ms: int = 5000):
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout_ms)
            if el:
                return el, sel
        except Exception:
            continue
    return None, None

def _fill_field(page, element, text: str):
    element.click()
    time.sleep(0.2)
    page.keyboard.press("Control+A")
    page.keyboard.type(text)
    time.sleep(0.4)

def _field_value(page, js_selector: str) -> str:
    try:
        val = page.evaluate(f'document.querySelector("{js_selector}")?.value || ""')
        return val or ""
    except Exception:
        return ""

# ── Selectors ─────────────────────────────────────────────────────────────────
CNR_SELECTORS = [
    "input[placeholder*='16 digit']",   # confirmed working — keep first
    "input[placeholder*='CNR number']",
    "input[placeholder*='CNR Number']",
    "input[placeholder*='digit CNR']",
    "input#cino",
    "input[name='cino']",
]

CAPTCHA_IMG_SELECTORS = (
    "img#captcha_image, img[src*='captcha'], img[alt*='captcha' i], "
    "img[src*='Captcha'], img[src*='generateCaptcha'], .captcha_image"
)

CAPTCHA_INPUT_SELECTORS = [
    "input[placeholder*='Enter Captcha']",  # confirmed working — moved to first
    "input[placeholder='*Enter Captcha']",  # was first, causes 5s timeout before fallback
    "input[placeholder*='Captcha']",
    "input#captcha",
    "input[name='captcha']",
]

CNR_JS_SEL = "input[placeholder*='16 digit'], input#cino, input[name='cino']"
CAP_JS_SEL = "input[placeholder*='Captcha'], input#captcha, input[name='captcha']"


# ── Fetch case history entries (clicks each date link) ────────────────────────
def fetch_history_details(page, max_entries: int = 2, logger=None) -> list:
    """
    From the case detail page, read the Case History table.
    Clicks the most recent `max_entries` 'Business on Date' links.
    Re-queries links fresh after every Back button click to avoid stale references.
    Returns list of dicts: {date, business, next_purpose, next_hearing_date}
    """
    _log = logger or print
    entries = []

    # Wall-clock guard: if the entire history fetch takes longer than this,
    # bail out and return whatever we have. No new threads needed — this just
    # checks time.time() at the top of each loop iteration.
    HISTORY_TIMEOUT_SECONDS = 60
    deadline = time.time() + HISTORY_TIMEOUT_SECONDS

    try:
        # Initial count to know how many links exist
        date_links = page.query_selector_all(
            "table:has(th:has-text('Business on Date')) td a, "
            "table:has(th:has-text('Hearing Date')) td a"
        )
        if not date_links:
            date_links = page.query_selector_all(
                ".case-history-table td a, table.history td a"
            )

        _log(f"  Found {len(date_links)} history date links")
        num_to_click = min(len(date_links), max_entries)

        for i in range(num_to_click):
            # Check wall-clock deadline before each iteration
            if time.time() > deadline:
                _log(f"  ⏰ History timeout after {HISTORY_TIMEOUT_SECONDS}s — stopping at {i} entries")
                break

            try:
                # Wait for history table to be present before re-querying
                # Critical on iteration 2+ after returning from Daily Status page
                try:
                    page.wait_for_selector(
                        "table:has(th:has-text('Business on Date')), "
                        "table:has(th:has-text('Hearing Date')), "
                        ".case-history-table, table.history",
                        timeout=8000
                    )
                    time.sleep(0.3)
                except Exception:
                    _log(f"  History table not visible before link {i+1}, proceeding anyway")

                # Re-query fresh — old references go stale after page reloads
                all_links = page.query_selector_all(
                    "table:has(th:has-text('Business on Date')) td a, "
                    "table:has(th:has-text('Hearing Date')) td a"
                )
                if not all_links:
                    all_links = page.query_selector_all(
                        ".case-history-table td a, table.history td a"
                    )

                if i >= len(all_links):
                    _log(f"  Link {i+1} not found after re-query, stopping")
                    break

                link = all_links[i]
                date_text = link.inner_text().strip()
                _log(f"  Clicking history link {i+1}: {date_text}")

                # Click — Daily Status loads on the same page, not a new tab
                link.click(timeout=5000, force=True)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                time.sleep(0.5)

                page_text = page.inner_text("body", timeout=10000) or ""

                business     = ""
                next_purpose = ""
                next_hearing = ""

                m = re.search(r'Business\s*:\s*(.+?)(?:\n|Next Purpose|$)', page_text, re.IGNORECASE)
                if m:
                    business = m.group(1).strip()

                m = re.search(r'Next Purpose\s*:\s*(.+?)(?:\n|Next Hearing|$)', page_text, re.IGNORECASE)
                if m:
                    next_purpose = m.group(1).strip()

                m = re.search(r'Next Hearing Date\s*:\s*(.+?)(?:\n|$)', page_text, re.IGNORECASE)
                if m:
                    next_hearing = m.group(1).strip()

                entries.append({
                    "date": date_text,
                    "business": business,
                    "next_purpose": next_purpose,
                    "next_hearing_date": next_hearing,
                })

                _log(f"  History entry: {date_text} → {business}")

                # Click the Daily Status page's own Back button (not #main_back_cnr
                # which belongs to the case page and is hidden here)
                try:
                    back_btn = page.wait_for_selector(
                        "button:has-text('Back'):not(#main_back_cnr), "
                        "a:has-text('Back'):not(#main_back_cnr), "
                        "input[value='Back']:not(#main_back_cnr)",
                        timeout=5000
                    )
                    back_btn.click(timeout=3000, force=True)
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    time.sleep(0.5)
                except Exception as e:
                    _log(f"  Back button not found, trying browser back: {e}")
                    page.go_back(timeout=10000)  # was missing timeout — could hang forever
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    time.sleep(0.5)

            except Exception as e:
                _log(f"  Error clicking history link {i+1}: {e}")
                try:
                    back_btn = page.query_selector(
                        "button:has-text('Back'):not(#main_back_cnr), "
                        "a:has-text('Back'):not(#main_back_cnr)"
                    )
                    if back_btn:
                        back_btn.click(timeout=3000, force=True)
                    else:
                        page.go_back(timeout=10000)  # was missing timeout
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    time.sleep(0.5)
                except Exception:
                    pass
                continue

    except Exception as e:
        _log(f"  Error fetching history details: {e}")

    return entries


# ── Server-side error-page detection ────────────────────────────────────────
# eCourts intermittently serves a blank page containing only this banner
# instead of the real CNR search form — observed after repeated requests in
# a short window (looks like a rate-limit / session throttle on their end).
# When this happens there is no CNR input to find at all, so the old code's
# immediate "Cannot find CNR input field on page" failure was really this in
# disguise — it gave up on attempt 1 instead of using the retry budget.
def _looks_like_error_page(page_text: str) -> bool:
    lp = (page_text or "").lower()
    return "search page not found" in lp or "welcome user" in lp


def _safe_close(browser):
    """
    Playwright's sync API is NOT thread-safe (it's built on greenlets tied to
    the calling thread), so browser.close() must be called directly from the
    same thread rather than via a background thread -- that approach caused
    'cannot switch to a different thread' greenlet errors. The retry cap on
    the error-page block (_MAX_ERROR_PAGE_RETRIES) is what actually keeps
    close() from being called on a badly-hung browser near the 280s mark;
    this is just a plain, safe wrapper.
    """
    try:
        browser.close()
    except Exception:
        pass


_MAX_ERROR_PAGE_RETRIES = 2  # fail fast — this block is sticky within a
                              # session but usually clears on the next case's
                              # fresh subprocess, so don't burn the full
                              # attempt/backoff budget chasing it here.


def _reload_ecourts(page, _log):
    page.goto(ECOURTS_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(1.5)
    try:
        page.click("text=CNR Number", timeout=5000)
        time.sleep(0.5)
    except Exception:
        pass


def fetch_case(page, cnr: str, logger=None, max_attempts: int = 5,
               detailed: bool = False) -> dict:
    """
    Fetch case details for a given CNR number.
    If detailed=True, also fetches the last 2 case history daily status entries.
    """
    _log = logger or print
    result = {
        "cnr": cnr,
        "raw_ok": False,
        "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    try:
        _log("  Loading eCourts...")
        page.goto(ECOURTS_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.5)

        try:
            page.click("text=CNR Number", timeout=5000)
            time.sleep(0.5)
        except Exception:
            pass

        error_page_hits = 0
        for attempt in range(1, max_attempts + 1):
            _log(f"  ── Attempt {attempt}/{max_attempts} at {datetime.now().strftime('%H:%M:%S')} ──")

            try:
                page.click(
                    "button.close, .close[data-dismiss], button[aria-label='Close']",
                    timeout=1000
                )
                time.sleep(0.2)
            except Exception:
                pass

            if attempt > 1:
                try:
                    page.click("text=CNR Number", timeout=3000)
                    time.sleep(0.5)
                except Exception:
                    pass

            cnr_el, cnr_sel = _find_input(page, CNR_SELECTORS, timeout_ms=8000)
            if not cnr_el:
                page_text_now = ""
                try:
                    page_text_now = page.inner_text("body", timeout=5000) or ""
                except Exception:
                    pass

                if _looks_like_error_page(page_text_now):
                    error_page_hits += 1
                    _log(f"  ⚠ eCourts served 'Search Page not Found' error page "
                         f"(hit {error_page_hits}/{_MAX_ERROR_PAGE_RETRIES})")

                    # This block tends to be sticky for the rest of THIS case's
                    # session (same browser/cookies) but often clears on the
                    # next case's fresh subprocess/session. Retrying heavily
                    # here mostly burns time and risks colliding with the
                    # 280s SIGALRM kill mid-navigation. Fail fast instead.
                    if error_page_hits >= _MAX_ERROR_PAGE_RETRIES:
                        result["error"] = "eCourts blocked this session (Search Page not Found)"
                        return result

                    backoff = 5 * error_page_hits  # 5s, then 10s
                    _log(f"  Backing off {backoff}s before retry...")
                    time.sleep(backoff)
                    _reload_ecourts(page, _log)
                    continue

                _log(f"  ⚠ CNR input not found, page may not have loaded fully (attempt {attempt}/{max_attempts})")
                if attempt == max_attempts:
                    result["error"] = "Cannot find CNR input field on page (after all retries)"
                    return result
                time.sleep(3)
                _reload_ecourts(page, _log)
                continue

            _log(f"  Found CNR input: {cnr_sel}")
            _fill_field(page, cnr_el, cnr.strip())
            _log(f"  Typed CNR: {cnr.strip()}")

            cnr_val = _field_value(page, CNR_JS_SEL)
            if len(cnr_val) < 10:
                _log(f"  ⚠ CNR field shows '{cnr_val}' — retyping")
                _fill_field(page, cnr_el, cnr.strip())

            captcha_text = ""
            cap_el = None
            try:
                # Wait for captcha image to appear in DOM
                page.wait_for_selector(CAPTCHA_IMG_SELECTORS, timeout=10000)

                # Fetch CAPTCHA bytes via JS fetch() instead of element.screenshot().
                # element.screenshot() is a heavy CDP render command that freezes when
                # Chromium is memory-starved after many cases on Render's free tier.
                # JS fetch() is a lightweight HTTP request — no rendering at all.
                #
                # AbortController inside the JS provides the 10s timeout because
                # page.evaluate() does NOT accept a timeout kwarg in this Playwright
                # version — the timeout must live inside the JS promise itself.
                _JS_FETCH_CAPTCHA = """
                async () => {
                    const img = document.querySelector(
                        'img#captcha_image, img[src*="captcha"], img[alt*="captcha" i], ' +
                        'img[src*="Captcha"], img[src*="generateCaptcha"], .captcha_image'
                    );
                    if (!img || !img.src) return null;
                    try {
                        const controller = new AbortController();
                        const tid = setTimeout(() => controller.abort(), 10000);
                        const resp = await fetch(img.src, {
                            credentials: 'include',
                            signal: controller.signal
                        });
                        clearTimeout(tid);
                        const buf = await resp.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = '';
                        for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
                        return btoa(bin);
                    } catch(e) { return null; }
                }
                """
                b64 = page.evaluate(_JS_FETCH_CAPTCHA)
                if not b64:
                    raise Exception("JS fetch returned null — captcha img not found or fetch failed")

                _log(f"  CAPTCHA fetched via JS ({len(b64)} b64 chars)")

                import base64 as _b64
                img_bytes = _b64.b64decode(b64)
                captcha_text = solve_captcha(img_bytes, _log)
                _log(f"  CAPTCHA solved: '{captcha_text}'")

            except Exception as e:
                _log(f"  CAPTCHA image not found: {e}")

            if captcha_text:
                cap_el, cap_sel = _find_input(page, CAPTCHA_INPUT_SELECTORS, timeout_ms=8000)
                if cap_el:
                    _log(f"  Found captcha input: {cap_sel}")
                    _fill_field(page, cap_el, captcha_text)
                else:
                    _log("  ⚠ Cannot find captcha input field")

            cnr_check = _field_value(page, CNR_JS_SEL)
            cap_check  = _field_value(page, CAP_JS_SEL)
            _log(f"  Pre-submit: CNR='{cnr_check}' | Captcha='{cap_check}'")

            if len(cnr_check) < 10:
                _log("  CNR empty before submit — retyping")
                if cnr_el:
                    _fill_field(page, cnr_el, cnr.strip())

            if not cap_check and captcha_text and cap_el:
                _log("  Captcha empty before submit — retyping")
                _fill_field(page, cap_el, captcha_text)

            submitted = False
            for btn_sel in [
                "button:has-text('Search')",
                "input[value='Search']",
                "button#searchbtn",
                "input#searchbtn",
                "button[type='submit']",
            ]:
                try:
                    page.click(btn_sel, timeout=1500, force=True)
                    submitted = True
                    _log(f"  Clicked submit: {btn_sel}")
                    break
                except Exception:
                    continue
            if not submitted:
                page.keyboard.press("Enter")
                _log("  Pressed Enter to submit")

            _log("  Waiting for results...")

            # Use wait_for_load_state instead of wait_for_selector here.
            # wait_for_selector can silently hang forever when the page does a
            # full navigation on submit (common on eCourts after wrong captcha):
            # it attaches to the old frame, that frame gets destroyed by the
            # navigation, and the timeout never fires because the timer is
            # orphaned on the dead frame's event loop.
            # wait_for_load_state is navigation-aware and handles this correctly.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                _log("  Timed out waiting for page load — reading page anyway")

            # Confirmed via debug screenshots: eCourts shows a "Loading..."
            # modal spinner while it fetches case details via AJAX AFTER
            # domcontentloaded fires -- and it was still stuck on screen
            # even after a 20s wait in production. Give it more room and
            # log how long it actually took, so we know whether it's just
            # slow or genuinely never resolves for automated sessions.
            _spinner_start = time.time()
            try:
                page.wait_for_selector("text=Loading...", state="hidden", timeout=40000)
                _elapsed = time.time() - _spinner_start
                if _elapsed > 1:
                    _log(f"  Loading spinner cleared after {_elapsed:.1f}s")
            except Exception:
                _log("  ⚠ 'Loading...' spinner never cleared after 40s — reading page anyway")
            time.sleep(1)

            page_text = ""
            try:
                page_text = page.inner_text("body", timeout=10000) or ""
            except Exception as e:
                _log(f"  Could not read page: {e}")
                continue

            lp = page_text.lower()
            if any(x in lp for x in ["invalid captcha", "wrong captcha",
                                      "captcha mismatch", "enter captcha"]):
                _log("  Wrong CAPTCHA — retrying")
                try:
                    page.click(
                        "img[onclick*='captcha'], a[onclick*='captcha'], "
                        ".refresh-captcha, i.fa-refresh",
                        timeout=4000
                    )
                    time.sleep(1)
                except Exception:
                    pass
                continue

            if any(x in lp for x in ["case not found", "no record found",
                                      "invalid cnr", "cnr not found"]):
                result["error"] = "Case not found — verify CNR is correct"
                return result

            # ── Parse: Next Hearing Date ──────────────────────────────────────
            next_date = "Not listed"
            for sel in [
                "td:has-text('Next Hearing Date') + td",
                "tr:has(td:has-text('Next Hearing Date')) td:nth-child(2)",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        txt = el.inner_text().strip()
                        if txt and len(txt) > 3:
                            next_date = txt
                            break
                except Exception:
                    continue

            if next_date == "Not listed":
                m = re.search(
                    r'Next Hearing Date[\s\S]{0,40}?'
                    r'(\d{2}(?:st|nd|rd|th)?\s+\w+\s+\d{4}|\d{2}[/-]\d{2}[/-]\d{4})',
                    page_text, re.IGNORECASE
                )
                if m:
                    next_date = m.group(1).strip()

            # ── Parse: Case Stage ─────────────────────────────────────────────
            stage = "Unknown"
            for sel in [
                "td:has-text('Case Stage') + td",
                "tr:has(td:has-text('Case Stage')) td:nth-child(2)",
                "td:has-text('Case Status') + td",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        txt = el.inner_text().strip()
                        if txt and len(txt) > 1:
                            stage = txt
                            break
                except Exception:
                    continue

            if stage == "Unknown":
                m = re.search(r'Case Stage[\s\S]{0,15}?([A-Z][A-Z /]{2,40})', page_text)
                if m:
                    stage = m.group(1).strip()

            # ── Parse: Petitioner & Advocate ──────────────────────────────────
            petitioner = ""
            for sel in [
                "div.Petitioner_and_Advocate_table td",
                "#petitioner_advocate",
                "table:has(th:has-text('Petitioner')) td",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        petitioner = el.inner_text().strip()[:150]
                        break
                except Exception:
                    continue

            if not petitioner:
                m = re.search(
                    r'Petitioner and Advocate\s*\n+([\s\S]{5,200}?)(?:\n\s*\n|Respondent)',
                    page_text
                )
                if m:
                    petitioner = " | ".join(m.group(1).split("\n")).strip()[:150]

            # ── Parse: Respondent & Advocate ──────────────────────────────────
            respondent = ""
            for sel in [
                "div.Respondent_and_Advocate_table td",
                "#respondent_advocate",
                "table:has(th:has-text('Respondent')) td",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        respondent = el.inner_text().strip()[:150]
                        break
                except Exception:
                    continue

            if not respondent:
                m = re.search(
                    r'Respondent and Advocate\s*\n+([\s\S]{5,200}?)(?:\n\s*\n|Acts|FIR|$)',
                    page_text
                )
                if m:
                    respondent = " | ".join(m.group(1).split("\n")).strip()[:150]

            # ── Parse: Court / Judge ──────────────────────────────────────────
            court = ""
            m = re.search(
                r'Court Number and Judge[\s\S]{0,20}?([\w ,\-\.]+(?:Judge|Court)[\w ,\-\.]*)',
                page_text
            )
            if m:
                court = m.group(1).strip()[:100]

            # ── Parse: Filing number ──────────────────────────────────────────
            filing_number = ""
            m = re.search(r'Filing Number[\s\S]{0,10}?(\d+/\d+)', page_text)
            if m:
                filing_number = m.group(1)

            # ── Success check ─────────────────────────────────────────────────
            if stage != "Unknown" or next_date != "Not listed" or petitioner:
                result.update({
                    "raw_ok": True,
                    "case_status": stage,
                    "next_hearing": next_date,
                    "petitioner": petitioner,
                    "respondent": respondent,
                    "court": court,
                    "filing_number": filing_number,
                })
                _log(f"  ✅ Stage: {stage} | Next: {next_date}")

                # ── Fetch detailed history if requested ───────────────────────
                if detailed:
                    _log("  Fetching case history details...")
                    history = fetch_history_details(page, max_entries=2, logger=_log)
                    result["history"] = history

                return result
            else:
                _log("  ⚠ Page loaded but no case data found — retrying")
                try:
                    import os as _os
                    _os.makedirs("/tmp/debug_no_data", exist_ok=True)
                    safe_cnr = re.sub(r'[^A-Za-z0-9]', '_', cnr)
                    page.screenshot(
                        path=f"/tmp/debug_no_data/{safe_cnr}_attempt{attempt}.png",
                        full_page=True
                    )
                    with open(f"/tmp/debug_no_data/{safe_cnr}_attempt{attempt}.html", "w", encoding="utf-8") as f:
                        f.write(page.content())
                    _log(f"  (debug screenshot/html saved for {cnr} attempt {attempt})")
                except Exception as e:
                    _log(f"  (debug capture failed: {e})")
                continue

        result["error"] = f"Could not retrieve case after {max_attempts} attempts"
        return result

    except PWTimeout:
        result["error"] = "Page timeout — eCourts may be slow or down"
        return result
    except Exception as e:
        result["error"] = str(e)[:200]
        return result


# How many cases to process per Chromium instance before restarting the browser.
# On Render's free tier (512MB RAM), Chromium accumulates memory across cases.
# By case 20+, it's memory-starved and CDP commands (like screenshot/fetch) freeze.
# Restarting every N cases keeps memory usage bounded.
_BROWSER_RECYCLE_EVERY = 1  # fresh browser per case — safest on 512MB Render free tier


def _make_browser_and_page(playwright_instance):
    """Launch a fresh Chromium browser and return (browser, page)."""
    browser = playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--mute-audio",
            # Memory reduction flags for Render free tier (512MB limit)
            "--single-process",            # renderer in same process, saves ~50MB
            "--no-zygote",                 # disables zygote process, saves ~20MB
            "--disable-javascript-harmony-shipping",
            "--js-flags=--max-old-space-size=128",  # cap V8 heap at 128MB
            "--media-cache-size=1",        # effectively disables media cache
            "--disk-cache-size=1",         # effectively disables disk cache
            "--disable-application-cache",
            "--disable-offline-load-stale-cache",
            "--disable-cache",
        ]
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-IN",
    )
    page = context.new_page()

    # Safety net: if Chromium freezes (memory pressure on Render free tier),
    # ANY CDP call will hang indefinitely. set_default_timeout ensures every
    # operation that doesn't have its own explicit timeout will raise
    # TimeoutError after 30s instead of hanging forever.
    # Operations with explicit timeouts (e.g. timeout=10000) keep their value.
    page.set_default_timeout(30000)

    page.on("dialog", lambda d: d.dismiss())
    return browser, page


def run_all_cases(cases: list, last_status: dict,
                  logger=None,
                  detailed: bool = False) -> tuple:
    """
    Run fetch_case for all cases. Returns (results, changes, new_status).

    Recycles the Chromium browser every _BROWSER_RECYCLE_EVERY cases to
    prevent memory exhaustion on long 'check all' runs. Also restarts the
    browser after any exception — a frozen/errored Chromium may still be in
    bad state and poison subsequent cases.
    page.set_default_timeout(30000) acts as a safety net for ALL CDP ops.
    """
    _log = logger or print
    results    = []
    changes    = []
    new_status = {}

    with sync_playwright() as p:
        browser, page = _make_browser_and_page(p)
        _log(f"Browser started (will recycle every {_BROWSER_RECYCLE_EVERY} cases)")

        for i, case in enumerate(cases):
            # Recycle browser every N cases to free accumulated memory
            if i > 0 and i % _BROWSER_RECYCLE_EVERY == 0:
                _safe_close(browser)
                browser, page = _make_browser_and_page(p)
                _log(f"♻️  Browser recycled at case {i+1}/{len(cases)}")

            cnr   = case["cnr"]
            label = case.get("label", cnr)
            _log(f"\nChecking: {label} ({cnr})")
            _log(f"  ⏱ Started at {datetime.now().strftime('%H:%M:%S')}")

            try:
                result = fetch_case(
                    page, cnr,
                    logger=_log, detailed=detailed
                )
            except Exception as e:
                _log(f"  ❌ Exception for {label}: {e}")
                result = {
                    "cnr": cnr,
                    "raw_ok": False,
                    "error": str(e)[:200],
                    "last_fetched": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                # Browser may be in bad/frozen state after an exception.
                # Restart it so the next case gets a clean instance.
                _log(f"  ♻️  Restarting browser after error")
                _safe_close(browser)
                try:
                    browser, page = _make_browser_and_page(p)
                except Exception as be:
                    _log(f"  ❌ Could not restart browser: {be}")

            result["label"] = label
            results.append(result)

            current_key = (
                result.get("case_status", "") + "|" +
                result.get("next_hearing", "")
            )
            prev_key = last_status.get(cnr, "")
            if prev_key and current_key != prev_key:
                changes.append(label)
                _log(f"  ⚡ CHANGE DETECTED for {label}!")

            new_status[cnr] = current_key
            _log(f"  ✔ Done with {label} at {datetime.now().strftime('%H:%M:%S')}")

        _safe_close(browser)

    return results, changes, new_status
