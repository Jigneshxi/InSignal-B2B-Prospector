from __future__ import annotations

"""
QA & Software Testing Prospecting Pipeline (Active within 60 Days)
------------------------------------------------------------------
Automates LinkedIn keyword signal collection for QA & Software Testing profiles.
Filters posts within a 60-day window, uses safe 20.0-second anti-ban throttling,
deduplicates prospects across queries, scores decision-makers, and outputs
both a clean CSV and formatted Excel workbook.
"""

import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output" / "qa_testing_60d"
RAW_DIR = OUTPUT_DIR / "raw"
FINAL_CSV = OUTPUT_DIR / "qa_testing_active_60d_prospects.csv"
FINAL_XLSX = OUTPUT_DIR / "qa_testing_active_60d_prospects.xlsx"
PROFILE_DIR = ROOT / ".li-profile"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERVICE_NAME = "QA & Software Testing Automation"

# High-intent QA & Testing Search Queries & Hashtags
QUERIES = [
    "QA Engineering",
    "QA automation",
    "software testing",
    "test automation",
    "API testing",
    "quality engineering",
    "web application testing",
    "performance testing",
    "#qaautomation",
    "#softwaretesting",
    "#testautomation",
    "#qualityengineering",
]

# Anti-ban Pacing & Recency Filtering
MAX_AGE_DAYS = 30                       # Active monthly lookback window (past-month)
MAX_POSTS_PER_QUERY = 10                # Posts to process per keyword
MAX_REACTOR_PAGES = 2                   # Reactor cursor pages to follow
MAX_COMMENT_PAGES = 2                   # UI comment unfolds per post
REQUEST_DELAY_SECONDS = 20.0            # Delay between internal API requests (Anti-Ban)
DELAY_BETWEEN_QUERIES_SECONDS = 20.0     # Pause between keyword searches
SEARCH_PAGE_SIZE = 3

# Decision-maker scoring rules
DECISION_MAKER_TERMS = [
    "qa director", "director of qa", "quality director", "head of qa",
    "head of quality", "qa manager", "quality manager", "lead qa",
    "qa lead", "test lead", "test manager", "vp engineering",
    "engineering director", "cto", "cio", "principal qa", "staff qa"
]

BUYING_INTENT_TERMS = [
    "hiring", "looking for", "evaluating", "recommendation",
    "tooling", "framework", "automation testing", "qa consulting"
]


def normalize(val: str | None) -> str:
    return " ".join(str(val or "").split()).strip()


def slugify(val: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "_" for c in val)
    return cleaned.strip("_")[:90] or "query"


def clean_profile_url(val: str) -> str:
    text = normalize(val)
    if not text:
        return ""
    try:
        p = urlparse(text)
        host = (p.hostname or "").lower()
        path = p.path.rstrip("/")
        if host in {"linkedin.com", "www.linkedin.com"} and path.lower().startswith("/in/"):
            return f"https://www.linkedin.com{path}"
    except Exception:
        pass
    return text


def score_prospect(headline: str, engagement: str, keywords: str, post_count: int) -> tuple[int, str]:
    hl = headline.lower()
    kw = (keywords + " " + hl).lower()

    score = 25  # Base score for matching active 60d signal

    # Engagement points
    if "posted" in engagement.lower():
        score += 25
    if "commented" in engagement.lower():
        score += 20
    if "reacted" in engagement.lower():
        score += 10

    # Decision-maker hits
    dm_hits = sum(1 for term in DECISION_MAKER_TERMS if term in hl)
    score += min(dm_hits * 15, 30)

    # Buying / Hiring intent hits
    intent_hits = sum(1 for term in BUYING_INTENT_TERMS if term in kw)
    score += min(intent_hits * 5, 10)

    # Activity volume
    score += min(post_count * 5, 15)
    score = min(score, 100)

    if score >= 70:
        tier = "HOT"
    elif score >= 50:
        tier = "WARM"
    else:
        tier = "NURTURE"

    return score, tier


def run_query(query: str, index: int) -> Path | None:
    output_file = RAW_DIR / f"{index:02d}_{slugify(query)}.csv"
    cmd = [
        sys.executable, "-m", "linkedin_listener.cli", "listen", query,
        "--out", str(output_file),
        "--max-age-days", str(MAX_AGE_DAYS),
        "--search-page-size", str(SEARCH_PAGE_SIZE),
        "--max-posts", str(MAX_POSTS_PER_QUERY),
        "--max-reactor-pages", str(MAX_REACTOR_PAGES),
        "--max-comment-pages", str(MAX_COMMENT_PAGES),
        "--request-delay", str(REQUEST_DELAY_SECONDS),
        "--delay", str(DELAY_BETWEEN_QUERIES_SECONDS),
        "--profile-dir", str(PROFILE_DIR),
    ]

    print(f"\n[{index}/{len(QUERIES)}] Searching: '{query}' (Last {MAX_AGE_DAYS} days)...")
    res = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False
    )

    if res.returncode == 0 and output_file.exists():
        return output_file
    print(f"  [!] Query '{query}' yielded no new records or encountered an error.")
    return None


def merge_and_enrich(files: list[Path]) -> list[dict]:
    merged: dict[str, dict] = {}

    for path in files:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                p_url = clean_profile_url(row.get("profile_url", ""))
                name = normalize(row.get("person_name", ""))
                if not p_url or not name or "/in/" not in p_url.lower():
                    continue

                if p_url not in merged:
                    merged[p_url] = {
                        "person_name": name,
                        "profile_url": p_url,
                        "headline": normalize(row.get("headline", "")),
                        "engagement_types": normalize(row.get("engagement_types", "")),
                        "reaction_types": normalize(row.get("reaction_types", "")),
                        "source_keywords": normalize(row.get("source_keywords", "")),
                        "source_post_urls": normalize(row.get("source_post_urls", "")),
                        "post_count": int(normalize(row.get("post_count", "1")) or 1),
                    }
                else:
                    item = merged[p_url]
                    for field in ("engagement_types", "reaction_types", "source_keywords", "source_post_urls"):
                        existing = [x.strip() for x in item[field].split(";") if x.strip()]
                        incoming = [x.strip() for x in normalize(row.get(field, "")).split(";") if x.strip()]
                        combined = list(dict.fromkeys(existing + incoming))
                        item[field] = "; ".join(combined)

                    urls = [x.strip() for x in item["source_post_urls"].split(";") if x.strip()]
                    item["post_count"] = max(len(urls), item["post_count"] + 1)

    # Score and rank prospects
    ranked = []
    for item in merged.values():
        score, tier = score_prospect(
            item["headline"],
            item["engagement_types"],
            item["source_keywords"],
            item["post_count"]
        )
        item["lead_score"] = score
        item["lead_tier"] = tier
        ranked.append(item)

    # Sort: Tier / Score descending, then name
    ranked.sort(key=lambda x: (-x["lead_score"], x["person_name"].lower()))
    return ranked


def export_excel(rows: list[dict], path: Path):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "QA Active Prospects (60d)"

        headers = [
            "Lead Tier", "Score", "Person Name", "Headline",
            "Engagement Types", "Reaction Types", "Profile URL",
            "Source Keywords", "Post Count", "Source Post URLs"
        ]
        ws.append(headers)

        header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        tier_colors = {
            "HOT": PatternFill(start_color="E6B8B7", end_color="E6B8B7", fill_type="solid"),
            "WARM": PatternFill(start_color="FDE9D9", end_color="FDE9D9", fill_type="solid"),
            "NURTURE": PatternFill(start_color="EBF1DE", end_color="EBF1DE", fill_type="solid")
        }

        for r_idx, row in enumerate(rows, start=2):
            ws.append([
                row["lead_tier"],
                row["lead_score"],
                row["person_name"],
                row["headline"],
                row["engagement_types"],
                row["reaction_types"],
                row["profile_url"],
                row["source_keywords"],
                row["post_count"],
                row["source_post_urls"]
            ])

            tier_cell = ws.cell(row=r_idx, column=1)
            if row["lead_tier"] in tier_colors:
                tier_cell.fill = tier_colors[row["lead_tier"]]
                tier_cell.alignment = Alignment(horizontal="center")

        # Auto fit column widths
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 50)

        wb.save(path)
        print(f"[EXCEL] Master workbook saved: {path}")
    except ImportError:
        pass


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(" QA & SOFTWARE TESTING SIGNAL MONITOR (60-DAY WINDOW)")
    print("=" * 70)

    # 1. Validate session
    check_cmd = [
        sys.executable, "-m", "linkedin_listener.cli", "--check",
        "--profile-dir", str(PROFILE_DIR)
    ]
    print("[PREFLIGHT] Checking LinkedIn browser session in .li-profile...")
    check_res = subprocess.run(
        check_cmd,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False
    )
    if check_res.returncode != 0:
        print("\n[!] LinkedIn session is not active or not logged in.")
        print("    Please log in first by running:")
        print("    linkedin-listener --login --profile-dir .li-profile\n")
        return 1

    print("[PREFLIGHT] Session validated successfully.")

    # 2. Run queries
    collected_files = []
    for idx, query in enumerate(QUERIES, 1):
        f = run_query(query, idx)
        if f:
            collected_files.append(f)
        if idx < len(QUERIES):
            import random
            pause_time = random.uniform(DELAY_BETWEEN_QUERIES_SECONDS * 0.9, DELAY_BETWEEN_QUERIES_SECONDS * 1.35)
            print(f"  [SAFE PACING] Waiting {pause_time:.1f}s to keep LinkedIn session safe...")
            time.sleep(pause_time)

    if not collected_files:
        print("[!] No raw data collected across queries.")
        return 1

    # 3. Merge & Deduplicate
    ranked_prospects = merge_and_enrich(collected_files)

    # 4. Write CSV
    csv_fields = [
        "lead_tier", "lead_score", "person_name", "profile_url",
        "headline", "engagement_types", "reaction_types",
        "source_keywords", "post_count", "source_post_urls"
    ]
    with FINAL_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        for r in ranked_prospects:
            writer.writerow(r)

    print("\n" + "=" * 70)
    print(f" PIPELINE COMPLETE: {SERVICE_NAME}")
    print("=" * 70)
    print(f"Total Unique Active Prospects Extracted: {len(ranked_prospects)}")
    print(f"Master CSV Output:   {FINAL_CSV}")

    # 5. Export Excel
    export_excel(ranked_prospects, FINAL_XLSX)

    # Also update qa people.csv in workspace root if requested
    root_qa_csv = ROOT / "qa people.csv"
    with root_qa_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        for r in ranked_prospects:
            writer.writerow(r)
    print(f"Updated Workspace:  {root_qa_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
