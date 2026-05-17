# ecourts-checker

Standalone daily eCourts case checker — no server, no Render, no chatbot.

Checks all your cases automatically at **10:00 AM IST** every day via GitHub Actions,
detects status changes, highlights upcoming hearings, and sends results to **Telegram**
and/or **Email**.

---

## How it works

```
GitHub Actions (cron 10 AM IST)
        │
        ▼
run_checker.py          ← orchestrator
  └── checker/run_cases.py   ← one subprocess per case (5-min timeout)
        └── checker/case_checker.py  ← Playwright + CAPTCHA solver (unchanged)
  └── storage.py        ← reads cases.json, persists last_status.json
  └── notifier.py       ← Telegram + HTML email
```

`last_status.json` is persisted between runs using **GitHub Actions cache**, so
change detection works across days without any database.

---

## Setup

### 1. Create the repo

```bash
# Fork / clone this repo, or push it as a new private repo on GitHub
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/YOUR_USERNAME/ecourts-checker.git
git push -u origin main
```

### 2. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name          | Value                                      | Required?   |
|---------------------|--------------------------------------------|-------------|
| `MONGODB_URI`        | Same Atlas connection string as the bot   | Recommended |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather                     | Optional    |
| `TELEGRAM_CHAT_ID`   | Your chat ID (use @userinfobot to get it) | Optional    |
| `EMAIL_SENDER`       | Your Gmail address                        | Optional    |
| `EMAIL_PASSWORD`     | Gmail **App Password** (not login pw)     | Optional    |
| `EMAIL_RECIPIENT`    | Address to receive reports                | Optional    |

> **MONGODB_URI** is the same string already in your Render/bot environment. With it set, the checker and the Telegram bot share the same cases list and status — add a case in the bot, it shows up here automatically. Without it, the checker falls back to the local `cases.json` file.
>
> At least one of Telegram or Email must be configured, or you'll get no notifications.

**Gmail App Password setup:**
1. Enable 2FA on your Google account
2. Go to myaccount.google.com → Security → App Passwords
3. Create one for "Mail" → copy the 16-character password

### 3. Enable GitHub Actions

Go to your repo → **Actions** tab → click "I understand my workflows, go ahead and enable them"

The workflow will now run automatically at **10:00 AM IST** every day.

---

## Running manually

### On GitHub (no laptop needed)

1. Go to **Actions** tab in your repo
2. Click **"eCourts Daily Case Check"** in the left sidebar
3. Click **"Run workflow"**
4. Choose options:
   - **Dry run** — checks cases, prints summary, skips notifications
   - **Skip email** — Telegram only
   - **Skip Telegram** — email only
5. Click **"Run workflow"** button

### On your laptop

```bash
# One-time setup
pip install -r requirements.txt
playwright install chromium --with-deps
# macOS/Linux: brew install tesseract  OR  sudo apt install tesseract-ocr

# Set credentials (or create a .env file and use `source .env`)
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export EMAIL_SENDER="you@gmail.com"
export EMAIL_PASSWORD="your_app_password"
export EMAIL_RECIPIENT="you@gmail.com"

# Run
python run_checker.py

# Options
python run_checker.py --dry-run    # check but don't notify
python run_checker.py --no-email   # Telegram only
python run_checker.py --no-tg      # email only
```

---

## Managing cases

Edit `cases.json` directly. Each entry needs a `cnr` and a `label`:

```json
[
  { "cnr": "TSMM110055622022", "label": "Harshavardhan case" },
  { "cnr": "TSRA140005882023", "label": "Amulya vs Sark" }
]
```

Commit and push — next run picks up the new list automatically.

---

## Notification contents

Both Telegram and email include:

- **Stats bar** — total cases, ok, failed, changes
- **🔴 Status Changes** — only cases where stage or next hearing date changed
- **📅 Upcoming Hearings** — any hearing within the next 7 days, highlighted by urgency
  - 🔴 TODAY / 🟠 Tomorrow / 🟡 N days away
- **All cases** — full list sorted by next hearing date (soonest first)

---

## Files

```
ecourts-checker/
├── .github/
│   └── workflows/
│       └── daily_check.yml   ← GitHub Actions schedule + manual dispatch
├── checker/
│   ├── case_checker.py       ← core scraper (Playwright + CAPTCHA) — unchanged
│   └── run_cases.py          ← subprocess entry point — unchanged
├── cases.json                ← your tracked cases (edit this)
├── last_status.json          ← auto-generated, gitignored, cached by Actions
├── run_checker.py            ← main orchestrator
├── storage.py                ← load/save cases and status
├── notifier.py               ← Telegram + email sender
├── requirements.txt
└── .gitignore
```

---

## Troubleshooting

**"No notification channels configured"**
→ Check that your secrets are set correctly in GitHub repo settings.

**Cases timing out**
→ eCourts is slow — 5-min timeout per case is already generous. GitHub Actions
has 7GB RAM so Chromium won't freeze like it did on Render's 512MB.

**"Invalid captcha" loops**
→ Tesseract is installed in the workflow. If it fails, the checker retries up to 5×.

**Change detection not working**
→ Status is stored in MongoDB Atlas (same DB as the bot). On the very first run
there's nothing to compare against, so no changes are reported — that's expected.
If MONGODB_URI is not set, the checker falls back to a local `last_status.json` file
which won't persist between GitHub Actions runs.
