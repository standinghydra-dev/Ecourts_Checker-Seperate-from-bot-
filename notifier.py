"""
notifier.py — Send case check results via Telegram and/or Email.

Reads credentials from environment variables. Either channel is optional;
if the relevant env vars are missing, that channel is silently skipped.

Telegram env vars:
  TELEGRAM_BOT_TOKEN   — bot token from @BotFather
  TELEGRAM_CHAT_ID     — your chat/group ID

Email (Gmail SMTP) env vars:
  EMAIL_SENDER         — your Gmail address
  EMAIL_PASSWORD       — Gmail App Password (not your login password)
  EMAIL_RECIPIENT      — address to send to (can be same as sender)
"""

import os
import smtplib
import logging
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import date, datetime

logger = logging.getLogger(__name__)

HEARING_SOON_DAYS = 7   # flag hearings within this many days in the summary


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_date(date_str: str):
    if not date_str or date_str in ("Not listed", "—", ""):
        return None
    cleaned = date_str.strip()
    for suffix in ["st", "nd", "rd", "th"]:
        cleaned = cleaned.replace(suffix + " ", " ")
    for fmt in ("%d %B %Y", "%d %b %Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


# ── Summary builder ───────────────────────────────────────────────────────────

def build_summary(results: list, changes: list, triggered_by: str = "scheduled") -> dict:
    """
    Returns a dict with:
      - 'telegram': HTML string for Telegram
      - 'email_subject': plain string
      - 'email_html': full HTML email body
      - 'email_plain': plain-text fallback
    """
    today        = date.today()
    changed_set  = set(changes)
    total        = len(results)
    ok_count     = sum(1 for r in results if r.get("raw_ok"))
    fail_count   = total - ok_count
    trigger_icon = "🔔" if triggered_by == "manual" else "🌅"

    # ── Upcoming hearings ──────────────────────────────────────────────────────
    upcoming = []
    for r in results:
        if not r.get("raw_ok"):
            continue
        hd = _parse_date(r.get("next_hearing", ""))
        if hd:
            days_away = (hd - today).days
            if 0 <= days_away <= HEARING_SOON_DAYS:
                upcoming.append({
                    "label":     r.get("label", r.get("cnr")),
                    "date_str":  r.get("next_hearing"),
                    "days_away": days_away,
                })
    upcoming.sort(key=lambda x: x["days_away"])

    # ── All cases sorted by next hearing (soonest first, then no-date) ─────────
    def _sort_key(r):
        hd = _parse_date(r.get("next_hearing", ""))
        return hd if hd else date(9999, 12, 31)

    sorted_results = sorted(results, key=_sort_key)

    # ─────────────────────────────────────────────────────────────────────────
    # TELEGRAM  (HTML parse mode)
    # ─────────────────────────────────────────────────────────────────────────
    lines = [
        f"{trigger_icon} <b>eCourts Daily Report — {today.strftime('%d %b %Y')}</b>",
        f"<i>Triggered by: {triggered_by}</i>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📁 {total} cases  |  ✅ {ok_count} ok  |  ❌ {fail_count} failed",
        "",
    ]

    # Changes
    if changed_set:
        lines.append("🔴 <b>Status Changes:</b>")
        for r in results:
            if r.get("label", r.get("cnr")) in changed_set:
                lines.append(
                    f"  • <b>{r.get('label','?')}</b> → {r.get('case_status','?')} "
                    f"| Next: {r.get('next_hearing','—')}"
                )
        lines.append("")
    else:
        lines.append("✅ <b>No status changes today</b>")
        lines.append("")

    # Upcoming hearings section
    if upcoming:
        lines.append(f"📅 <b>Upcoming Hearings (next {HEARING_SOON_DAYS} days):</b>")
        for u in upcoming:
            if u["days_away"] == 0:
                when = "🔴 <b>TODAY</b>"
            elif u["days_away"] == 1:
                when = "🟠 <b>Tomorrow</b>"
            else:
                when = f"🟡 In {u['days_away']} days"
            lines.append(f"  • <b>{u['label']}</b> — {u['date_str']}  {when}")
        lines.append("")
    else:
        lines.append(f"📅 No hearings in the next {HEARING_SOON_DAYS} days")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>All Cases:</b>")
    lines.append("")

    for r in sorted_results:
        cnr     = r.get("cnr", "")
        label   = r.get("label", cnr)
        changed = label in changed_set

        if not r.get("raw_ok"):
            lines.append(f"⚠️ <b>{label}</b>  <code>{cnr}</code>")
            lines.append(f"   ❌ {r.get('error','?')}")
        else:
            icon = "🔴" if changed else "✅"
            stage = r.get("case_status", "—")
            nxt   = r.get("next_hearing", "—")
            hd    = _parse_date(nxt)
            days_badge = ""
            if hd:
                days_away = (hd - today).days
                if days_away == 0:
                    days_badge = "  🔴 TODAY"
                elif days_away == 1:
                    days_badge = "  🟠 Tomorrow"
                elif days_away <= HEARING_SOON_DAYS:
                    days_badge = f"  🟡 {days_away}d away"

            lines.append(f"{icon} <b>{label}</b>  <code>{cnr}</code>")
            lines.append(f"   📋 {stage}  |  📅 {nxt}{days_badge}")
            if changed:
                lines.append("   🔴 <i>Status changed since last check</i>")

        lines.append("")

    tg_text = "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────────────
    # EMAIL
    # ─────────────────────────────────────────────────────────────────────────
    change_count = len(changed_set)
    subject = (
        f"eCourts Update {today.strftime('%d %b')} — "
        f"{change_count} change{'s' if change_count != 1 else ''}"
        + (f", {len(upcoming)} hearing{'s' if len(upcoming) != 1 else ''} soon" if upcoming else "")
    )

    # HTML email
    def _status_badge(r):
        if not r.get("raw_ok"):
            return '<span style="color:#e53e3e">❌ Error</span>'
        changed = r.get("label", r.get("cnr")) in changed_set
        return '<span style="color:#e53e3e">🔴 Changed</span>' if changed else '<span style="color:#38a169">✅ OK</span>'

    def _days_badge_html(r):
        hd = _parse_date(r.get("next_hearing", ""))
        if not hd:
            return ""
        days_away = (hd - today).days
        if days_away < 0:
            return ""
        if days_away == 0:
            return ' <span style="background:#e53e3e;color:white;padding:1px 6px;border-radius:4px;font-size:11px">TODAY</span>'
        if days_away == 1:
            return ' <span style="background:#dd6b20;color:white;padding:1px 6px;border-radius:4px;font-size:11px">Tomorrow</span>'
        if days_away <= HEARING_SOON_DAYS:
            return f' <span style="background:#d69e2e;color:white;padding:1px 6px;border-radius:4px;font-size:11px">{days_away}d</span>'
        return ""

    upcoming_html = ""
    if upcoming:
        rows = ""
        for u in upcoming:
            color = "#e53e3e" if u["days_away"] == 0 else "#dd6b20" if u["days_away"] == 1 else "#d69e2e"
            when  = "TODAY" if u["days_away"] == 0 else "Tomorrow" if u["days_away"] == 1 else f"In {u['days_away']} days"
            rows += f"""
            <tr>
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0"><b>{u['label']}</b></td>
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">{u['date_str']}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">
                <span style="background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:12px">{when}</span>
              </td>
            </tr>"""
        upcoming_html = f"""
        <h2 style="color:#2d3748;margin-top:28px">📅 Upcoming Hearings (next {HEARING_SOON_DAYS} days)</h2>
        <table style="width:100%;border-collapse:collapse;background:#fffbeb;border:1px solid #f6e05e;border-radius:6px">
          <thead>
            <tr style="background:#fefce8">
              <th style="padding:8px 12px;text-align:left;color:#744210">Case</th>
              <th style="padding:8px 12px;text-align:left;color:#744210">Hearing Date</th>
              <th style="padding:8px 12px;text-align:left;color:#744210">When</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    changes_html = ""
    if changed_set:
        rows = ""
        for r in results:
            if r.get("label", r.get("cnr")) in changed_set:
                rows += f"""
                <tr>
                  <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0"><b>{r.get('label','?')}</b></td>
                  <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0"><code>{r.get('cnr','')}</code></td>
                  <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">{r.get('case_status','—')}</td>
                  <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">{r.get('next_hearing','—')}</td>
                </tr>"""
        changes_html = f"""
        <h2 style="color:#c53030;margin-top:28px">🔴 Status Changes</h2>
        <table style="width:100%;border-collapse:collapse;background:#fff5f5;border:1px solid #fc8181;border-radius:6px">
          <thead>
            <tr style="background:#fff0f0">
              <th style="padding:8px 12px;text-align:left;color:#742a2a">Case</th>
              <th style="padding:8px 12px;text-align:left;color:#742a2a">CNR</th>
              <th style="padding:8px 12px;text-align:left;color:#742a2a">New Stage</th>
              <th style="padding:8px 12px;text-align:left;color:#742a2a">Next Hearing</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    all_cases_rows = ""
    for r in sorted_results:
        cnr     = r.get("cnr", "")
        label   = r.get("label", cnr)
        changed = label in changed_set
        bg      = "#fff5f5" if changed else "white"
        if not r.get("raw_ok"):
            all_cases_rows += f"""
            <tr style="background:{bg}">
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0"><b>{label}</b></td>
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0"><code style="font-size:11px">{cnr}</code></td>
              <td colspan="2" style="padding:6px 12px;border-bottom:1px solid #e2e8f0;color:#e53e3e">❌ {r.get('error','?')}</td>
            </tr>"""
        else:
            all_cases_rows += f"""
            <tr style="background:{bg}">
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">
                {_status_badge(r)} <b>{label}</b>
                {'<br><small style="color:#c53030">🔴 Changed</small>' if changed else ""}
              </td>
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0"><code style="font-size:11px">{cnr}</code></td>
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">{r.get('case_status','—')}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">{r.get('next_hearing','—')}{_days_badge_html(r)}</td>
            </tr>"""

    email_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;padding:20px;color:#2d3748">

  <div style="background:linear-gradient(135deg,#2b6cb0,#2c5282);color:white;padding:24px;border-radius:8px;margin-bottom:24px">
    <h1 style="margin:0;font-size:22px">⚖️ eCourts Case Update</h1>
    <p style="margin:8px 0 0;opacity:0.85">{today.strftime('%A, %d %B %Y')} &nbsp;·&nbsp; {triggered_by.title()}</p>
  </div>

  <div style="display:flex;gap:16px;margin-bottom:24px">
    <div style="flex:1;background:#ebf8ff;border:1px solid #bee3f8;border-radius:6px;padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#2b6cb0">{total}</div>
      <div style="color:#4a5568;font-size:13px">Total Cases</div>
    </div>
    <div style="flex:1;background:#f0fff4;border:1px solid #9ae6b4;border-radius:6px;padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#276749">{ok_count}</div>
      <div style="color:#4a5568;font-size:13px">Fetched OK</div>
    </div>
    <div style="flex:1;background:#fff5f5;border:1px solid #fc8181;border-radius:6px;padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#c53030">{fail_count}</div>
      <div style="color:#4a5568;font-size:13px">Failed</div>
    </div>
    <div style="flex:1;background:#fffbeb;border:1px solid #f6e05e;border-radius:6px;padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#744210">{change_count}</div>
      <div style="color:#4a5568;font-size:13px">Changes</div>
    </div>
  </div>

  {upcoming_html}
  {changes_html}

  <h2 style="color:#2d3748;margin-top:28px">📋 All Cases</h2>
  <table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0;border-radius:6px">
    <thead>
      <tr style="background:#edf2f7">
        <th style="padding:8px 12px;text-align:left;color:#4a5568">Case</th>
        <th style="padding:8px 12px;text-align:left;color:#4a5568">CNR</th>
        <th style="padding:8px 12px;text-align:left;color:#4a5568">Stage</th>
        <th style="padding:8px 12px;text-align:left;color:#4a5568">Next Hearing</th>
      </tr>
    </thead>
    <tbody>{all_cases_rows}</tbody>
  </table>

  <p style="color:#a0aec0;font-size:12px;margin-top:24px;text-align:center">
    Generated by ecourts-checker · {datetime.now().strftime('%Y-%m-%d %H:%M IST')}
  </p>
</body>
</html>"""

    # Plain text fallback
    plain_lines = [
        f"eCourts Case Update — {today.strftime('%d %b %Y')}",
        f"Triggered by: {triggered_by}",
        f"Total: {total}  OK: {ok_count}  Failed: {fail_count}  Changes: {change_count}",
        "",
    ]
    if changed_set:
        plain_lines.append("STATUS CHANGES:")
        for r in results:
            if r.get("label", r.get("cnr")) in changed_set:
                plain_lines.append(f"  * {r.get('label')} -> {r.get('case_status')} | {r.get('next_hearing')}")
        plain_lines.append("")
    if upcoming:
        plain_lines.append(f"UPCOMING HEARINGS (next {HEARING_SOON_DAYS} days):")
        for u in upcoming:
            when = "TODAY" if u["days_away"] == 0 else "Tomorrow" if u["days_away"] == 1 else f"In {u['days_away']} days"
            plain_lines.append(f"  * {u['label']} — {u['date_str']} ({when})")
        plain_lines.append("")
    plain_lines.append("ALL CASES:")
    for r in sorted_results:
        label = r.get("label", r.get("cnr"))
        if not r.get("raw_ok"):
            plain_lines.append(f"  [ERROR] {label}: {r.get('error','?')}")
        else:
            plain_lines.append(
                f"  {'[CHANGED]' if label in changed_set else '[OK]'} "
                f"{label} | {r.get('case_status','—')} | Next: {r.get('next_hearing','—')}"
            )

    return {
        "telegram":    tg_text,
        "email_subject": subject,
        "email_html":  email_html,
        "email_plain": "\n".join(plain_lines),
    }


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("Telegram not configured — skipping")
        return False

    api = f"https://api.telegram.org/bot{token}/sendMessage"
    MAX = 4096

    # Split into chunks at blank lines so we don't cut mid-line
    chunks, current = [], []
    for line in text.split("\n"):
        if sum(len(l) + 1 for l in current) + len(line) + 1 > MAX:
            chunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current))

    all_ok = True
    for chunk in chunks:
        resp = requests.post(api, json={
            "chat_id":    chat_id,
            "text":       chunk,
            "parse_mode": "HTML",
        }, timeout=30)
        if not resp.ok:
            logger.error("Telegram send failed: %s", resp.text)
            all_ok = False
    return all_ok


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, plain_body: str) -> bool:
    sender    = os.environ.get("EMAIL_SENDER", "")
    password  = os.environ.get("EMAIL_PASSWORD", "")
    recipient = os.environ.get("EMAIL_RECIPIENT", "")
    if not sender or not password or not recipient:
        logger.info("Email not configured — skipping")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"eCourts Checker <{sender}>"
    msg["To"]      = recipient

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body,  "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        logger.info("Email sent to %s", recipient)
        return True
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False


# ── Main dispatch ─────────────────────────────────────────────────────────────

def notify(results: list, changes: list, triggered_by: str = "scheduled"):
    """Build summary and send via all configured channels."""
    payload = build_summary(results, changes, triggered_by)

    tg_ok    = send_telegram(payload["telegram"])
    email_ok = send_email(
        payload["email_subject"],
        payload["email_html"],
        payload["email_plain"],
    )

    if not tg_ok and not email_ok:
        logger.warning("No notification channels configured or all failed!")
    return tg_ok, email_ok
