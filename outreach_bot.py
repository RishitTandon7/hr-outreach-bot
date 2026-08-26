#!/usr/bin/env python3
"""
Outreach bot — sends personalized cold emails to HR contacts from contacts.csv,
updates the tracker in place, and stops once the hourly BATCH_SIZE is hit.

Designed to be triggered once per hour by GitHub Actions (see
.github/workflows/outreach.yml). Each run:
  1. Loads contacts.csv
  2. Picks the next BATCH_SIZE rows where Status is empty (already sorted by
     priority tier, so Tier 1 always goes first)
  3. Asks Gemini for one custom personalization line per company
  4. Sends the email via Gmail SMTP with your resume attached
  5. Writes Status=Sent, Date Contacted, and a Follow-up Date (+6 days) back
     into contacts.csv, then commits the file (done by the workflow, not here)

Required environment variables (set as GitHub Actions secrets):
  GMAIL_ADDRESS        - the Gmail address you're sending from
  GMAIL_APP_PASSWORD   - a Gmail App Password (not your normal password)
  GEMINI_API_KEYS      - comma-separated list of Gemini API keys to rotate through
  RESUME_PATH          - path to your resume PDF in the repo (e.g. resume.pdf)

Optional:
  BATCH_SIZE           - contacts to send this run (default 10)
  DRY_RUN              - if "true", generates + prints emails but does not send
"""

import os
import re
import csv
import time
import random
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
import urllib.request
import json

CSV_PATH = os.environ.get("CONTACTS_CSV", "contacts.csv")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
RESUME_PATH = os.environ.get("RESUME_PATH", "resume.pdf")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
GEMINI_API_KEYS = [k.strip() for k in re.split(r"[,\s]+", os.environ.get("GEMINI_API_KEYS", "")) if k.strip()]

YOUR_NAME = "Rishit Tandon"
YOUR_PHONE = "+91 7394865520"
YOUR_LINKEDIN = "linkedin.com/in/rishit-tandon-928661287"
YOUR_GITHUB = "github.com/RishitTandon7"
YOUR_PORTFOLIO = "portfolio.rishit.site"

PROFILE_SNAPSHOT = """- Research Intern (ML & Image Processing) at SRM IST x NIDM, building a satellite-based Glacial Lake Outburst Flood (GLOF) early-warning pipeline
- Prior SDE internship at DGTL Innovations
- 12+ national/international hackathon wins, including 1st place at RoboRoarZ Singapore 2024 and SEISMO HACK 1.0
- 80-90+ public projects on GitHub spanning AI/ML, full-stack, and robotics/IoT"""

EMAIL_BODY_TEMPLATE = """Hi {first_name},

Hope you're doing well. I know inboxes like yours get a lot of these, so I'll keep it brief.

I'm {your_name}, a final-year B.Tech Computer Science student at SRM Institute of Science and Technology (graduating 2027), currently looking for full-time AI/ML Engineer or SDE opportunities.

A quick snapshot of my profile:
{profile_snapshot}

{custom_line}

I'd be grateful if you could consider my profile for any current or upcoming openings at {company}. I've attached my resume for reference.

Thank you for your time — I really appreciate it.

Best regards,
{your_name}
{your_phone}
{your_linkedin}
{your_github}
{your_portfolio}"""


# Only models with nonzero RPM quota on the account's AI Studio rate-limit
# page — models showing 0/0 there aren't enabled for this account/tier.
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
]
MAX_GEMINI_ATTEMPTS = 12


def gemini_custom_line(company, title):
    """Ask Gemini for one personalized sentence about the company, rotating
    across both API keys and models to spread load and dodge per-model rate
    limits. Falls back to a generic line if every attempt fails or no keys
    are configured."""
    fallback = f"I'd love the chance to contribute to {company}'s engineering team."
    if not GEMINI_API_KEYS:
        return fallback

    prompt = (
        f"Write ONE short, genuine-sounding sentence (max 25 words) for a cold "
        f"outreach email to a company called '{company}', where the recipient's "
        f"title is '{title}'. The sentence should show light familiarity with "
        f"what a company like this likely does, and express interest in "
        f"contributing there as an AI/ML or software engineer. No fluff, no "
        f"hashtags, no quotation marks, just the sentence."
    )

    attempts = [(key, model) for key in GEMINI_API_KEYS for model in GEMINI_MODELS]
    random.shuffle(attempts)
    for key, model in attempts[:MAX_GEMINI_ATTEMPTS]:
        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={key}"
            )
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}]
            }).encode()
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if text:
                return text
        except Exception as e:
            print(f"  [gemini key ...{key[-4:]} / {model} failed: {e}]")
            continue
    return fallback


def send_email(to_email, subject, body, resume_path):
    if DRY_RUN:
        print(f"  [DRY RUN] Would send to {to_email}\n---\n{body}\n---")
        return True
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("  Missing GMAIL_ADDRESS / GMAIL_APP_PASSWORD — skipping send.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_email
    msg.set_content(body)

    if os.path.exists(resume_path):
        with open(resume_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="application",
                subtype="pdf",
                filename=os.path.basename(resume_path),
            )

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"  Send failed for {to_email}: {e}")
        return False


def main():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())

    pending = [r for r in rows if not r.get("Status", "").strip()]
    print(f"Pending contacts: {len(pending)} / {len(rows)}")

    batch = pending[:BATCH_SIZE]
    if not batch:
        print("No pending contacts left. All done!")
        return

    sent_count = 0
    for row in batch:
        company = row["Company"]
        title = row["Title"]
        name = row["Name"]
        email = row["Email"]
        first_name = name.split()[0] if name else "there"

        print(f"-> {name} ({title}) at {company} <{email}>")
        custom_line = gemini_custom_line(company, title)

        body = EMAIL_BODY_TEMPLATE.format(
            first_name=first_name,
            your_name=YOUR_NAME,
            profile_snapshot=PROFILE_SNAPSHOT,
            custom_line=custom_line,
            company=company,
            your_phone=YOUR_PHONE,
            your_linkedin=YOUR_LINKEDIN,
            your_github=YOUR_GITHUB,
            your_portfolio=YOUR_PORTFOLIO,
        )
        subject = f"Application for AI/ML & SDE Roles — {YOUR_NAME} (Final Year, SRM)"

        ok = send_email(email, subject, body, RESUME_PATH)
        today = datetime.now().strftime("%Y-%m-%d")
        followup = (datetime.now() + timedelta(days=6)).strftime("%Y-%m-%d")

        if ok:
            row["Status"] = "Sent"
            row["Date Contacted"] = today
            row["Follow-up Date"] = followup
            sent_count += 1
        else:
            row["Status"] = "Bounced" if not DRY_RUN else "Not Started"

        # gentle pacing between sends within the batch (avoid spam-pattern bursts)
        time.sleep(random.uniform(3, 8))

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"Done. Sent {sent_count}/{len(batch)} this run.")


if __name__ == "__main__":
    main()
