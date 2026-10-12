from __future__ import annotations

"""
Safe Top-Decision-Maker Contact-Info Inspector
----------------------------------------------
Safely inspects ONLY the highest-priority "HOT" decision-makers (capped at 20-25)
to extract direct emails, phone numbers, and portfolio links from their
LinkedIn Contact Info overlay.

Safety Controls Enforced:
1. Hard daily cap: Maximum 25 profiles per run (avoids LinkedIn commercial use limits).
2. Human jitter delay: 35.0 to 55.0 seconds between profile visits.
3. Uses existing persistent session (.li-profile).
4. Updates both CSV and Excel outputs seamlessly.
"""

import csv
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / ".li-profile"
ENRICHED_CSV = ROOT / "output" / "qa_testing_60d" / "qa_testing_enriched_contacts.csv"
ENRICHED_XLSX = ROOT / "output" / "qa_testing_60d" / "qa_testing_enriched_contacts.xlsx"

MAX_HOT_PROFILES_TO_INSPECT = 20
MIN_DELAY_SECONDS = 35.0
MAX_DELAY_SECONDS = 55.0

EMAIL_REGEX = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
PHONE_REGEX = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}|\+?91[-.\s]?\d{10}|\b\d{10}\b")


def safe_sleep(min_sec: float = MIN_DELAY_SECONDS, max_sec: float = MAX_DELAY_SECONDS):
    duration = random.uniform(min_sec, max_sec)
    print(f"  [SAFE PACING] Pausing {duration:.1f}s to mimic natural human reading...")
    time.sleep(duration)


def extract_slug(url: str) -> str:
    path = urlparse(url or "").path.rstrip("/")
    if "/in/" in path:
        return path.split("/in/")[-1].strip("/")
    return ""


def inspect_profile_contact_info(page, profile_url: str) -> dict[str, str]:
    slug = extract_slug(profile_url)
    if not slug:
        return {}

    contact_overlay_url = f"https://www.linkedin.com/in/{slug}/overlay/contact-info/"
    print(f"\n[INSPECT] Opening: {slug}...")

    try:
        # Load contact info overlay directly
        nav = page.goto(contact_overlay_url, wait_until="domcontentloaded", timeout=45000)
        if nav and nav.status == 429:
            print("  [!] HTTP 429 received. Throttling active.")
            return {"throttled": "true"}

        page.wait_for_timeout(random.randint(2500, 4000))

        content_text = page.locator("section.pv-contact-info__contact-type, div.artdeco-modal__content").all_inner_texts()
        full_text = " ".join(content_text)

        emails = EMAIL_REGEX.findall(full_text)
        phones = PHONE_REGEX.findall(full_text)

        # Website links
        links = page.locator("div.artdeco-modal__content a").all()
        extracted_sites = []
        for l in links:
            href = l.get_attribute("href") or ""
            if href and "linkedin.com" not in href and "javascript" not in href:
                extracted_sites.append(href)

        email_val = emails[0] if emails else ""
        phone_val = phones[0] if phones else ""
        site_val = "; ".join(dict.fromkeys(extracted_sites))

        if email_val:
            print(f"  [+] DIRECT EMAIL FOUND: {email_val}")
        if phone_val:
            print(f"  [+] DIRECT PHONE FOUND: {phone_val}")
        if site_val:
            print(f"  [+] SITES/PORTFOLIO: {site_val}")

        if not email_val and not phone_val and not site_val:
            print("  [-] Contact info is private (hidden behind connection wall).")

        return {
            "email": email_val,
            "phone": phone_val,
            "websites": site_val
        }
    except Exception as e:
        print(f"  [!] Notice during inspection of {slug}: {e}")
        return {}


def main():
    if not ENRICHED_CSV.exists():
        print(f"[!] Target file not found: {ENRICHED_CSV}")
        return 1

    with ENRICHED_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    # Filter to Top HOT Decision-Makers
    hot_candidates = []
    for r in rows:
        if r.get("lead_tier") == "HOT" and r.get("profile_url"):
            # Prioritize those where email isn't already directly verified
            if r.get("confidence") != "Verified":
                hot_candidates.append(r)

    print("=" * 70)
    print(" SAFE TOP-DECISION-MAKER CONTACT-INFO INSPECTOR")
    print("=" * 70)
    print(f"Total HOT Decision-Makers eligible: {len(hot_candidates)}")
    print(f"Safety Limit: Max {MAX_HOT_PROFILES_TO_INSPECT} profiles this session")
    print(f"Pacing: {MIN_DELAY_SECONDS}s to {MAX_DELAY_SECONDS}s randomized human pause")

    to_process = hot_candidates[:MAX_HOT_PROFILES_TO_INSPECT]
    if not to_process:
        print("[!] No eligible unverified HOT candidates found.")
        return 0

    from linkedin_listener.session import LinkedInSession

    updated_count = 0
    with LinkedInSession(str(PROFILE_DIR), headless=True) as seat:
        st = seat.session_status()
        if st != 200:
            print(f"[!] LinkedIn session is not active (status {st}). Run --login first.")
            return 1

        for idx, cand in enumerate(to_process, 1):
            name = cand.get("person_name")
            p_url = cand.get("profile_url")
            print(f"\n[{idx}/{len(to_process)}] Inspecting: {name} (HOT Tier)")

            info = inspect_profile_contact_info(seat.page, p_url)
            if info.get("throttled"):
                print("[!] Safe early exit due to throttling signal.")
                break

            if info.get("email"):
                cand["primary_email"] = info["email"]
                cand["email_type"] = "Direct LinkedIn Contact-Info"
                cand["confidence"] = "Verified"
                cand["contact_source"] = "LinkedIn Contact Overlay"
                updated_count += 1

            if info.get("phone"):
                cand["phone_number"] = info["phone"]
                updated_count += 1

            if idx < len(to_process):
                safe_sleep()

    # Save updated data back to CSV and Excel
    if updated_count > 0:
        fieldnames = list(rows[0].keys())
        with ENRICHED_CSV.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)

        # Sync with open file
        active_csv = ROOT / "output" / "qa_testing_60d" / "qa_testing_active_60d_prospects.csv"
        with active_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)

        # Re-export Excel
        from enrich_qa_prospects_contacts import export_excel
        export_excel(rows, ENRICHED_XLSX)

        print("\n" + "=" * 70)
        print(f" SAFE INSPECTION COMPLETE: {updated_count} New Direct Contacts Added!")
        print("=" * 70)
    else:
        print("\n[INFO] Inspection finished. No new public contact info found beyond privacy walls.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
