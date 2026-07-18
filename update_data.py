#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_data.py — i20.co.uk weekly content pipeline (v2).

Runs unattended every Monday via GitHub Actions:
  1. FETCH  — pulls latest ONS CSV time series data.
  2. CHART  — generates coloured area charts from historical data (matplotlib).
  3. WRITE  — sends figures + topic to gpt-4o-mini for ~500-word analysis.
  4. INJECT — rewrites index.html article bodies, card teasers and timestamp.
  5. SAVE   — atomic write so a crash never corrupts the live site.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import requests
from openai import OpenAI

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
CHARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "charts")

OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TIMEOUT_SECONDS = 90
HTTP_TIMEOUT_SECONDS = 20
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 4

SYSTEM_PROMPT = (
    "Act as an expert British societal sociologist writing for an informed "
    "general audience. Write an authoritative analytical article of approximately "
    "500 words evaluating how this specific statistic impacts modern UK life. "
    "Use British English spelling throughout. Write four flowing paragraphs "
    "separated by double newlines. Each paragraph should transition naturally "
    "into the next — do NOT begin any paragraph with stock phrases like "
    "'Looking ahead', 'In conclusion', 'Moving forward', 'Turning to', or "
    "'It is worth noting'. Vary your openings: use concrete observations, "
    "rhetorical questions, historical comparisons, or striking details. "
    "The article should move from context to evidence to implications to "
    "outlook, but the structure must feel organic, not labelled.\n\n"
    "Do NOT include any headings, labels, bullet points or markdown. "
    "Respond with the article text only."
)

# ONS CSV download — stable public endpoint.
ONS_CSV = "https://www.ons.gov.uk/generator?format=csv&uri={uri}"

STAT_SOURCES = {
    "cpih_inflation": {
        "label": "CPIH annual inflation rate (%)",
        "uri": "/economy/inflationandpriceindices/timeseries/l55o/mm23",
    },
    "unemployment": {
        "label": "UK unemployment rate, ages 16+ (%)",
        "uri": "/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms",
    },
    "employment_rate": {
        "label": "UK employment rate, ages 16-64 (%)",
        "uri": "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/timeseries/lf24/lms",
    },
    "economic_inactivity": {
        "label": "UK economic inactivity rate, ages 16-64 (%)",
        "uri": "/employmentandlabourmarket/peoplenotinwork/economicinactivity/timeseries/lf2s/lms",
    },
    "gdp_growth": {
        "label": "UK GDP quarter-on-quarter growth (%)",
        "uri": "/economy/grossdomesticproductgdp/timeseries/ihyq/pn2",
    },
    "vacancies": {
        "label": "UK job vacancies, total (thousands)",
        "uri": "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/timeseries/ap2y/unem",
    },
}

# Sector accent colours — must match CSS custom properties in index.html
SECTOR_COLORS = {
    "Economy":    "#1e3a5f",
    "Technology": "#0d9488",
    "Healthcare": "#e8634a",
    "Education":  "#d97706",
    "Housing":    "#2d8a56",
    "Politics":   "#6C3483",
}

# Maps card number → stat key (which ONS series anchors which card)
TOPICS = [
    {"id": "01", "title": "High Street Bank Deserts",            "sector": "Economy",    "stat": "gdp_growth"},
    {"id": "02", "title": "The Turnout Crisis",                  "sector": "Politics",   "stat": "economic_inactivity"},
    {"id": "03", "title": "The Regional Productivity Divide",    "sector": "Economy",    "stat": "gdp_growth"},
    {"id": "04", "title": "The Gig Economy Pension Void",        "sector": "Economy",    "stat": "employment_rate"},
    {"id": "05", "title": "The Rural Broadband Last Mile",       "sector": "Technology", "stat": "gdp_growth"},
    {"id": "06", "title": "Digital-Only by Default",             "sector": "Technology", "stat": "economic_inactivity"},
    {"id": "07", "title": "AI and the Entry-Level Job",          "sector": "Technology", "stat": "vacancies"},
    {"id": "08", "title": "Britain's Data Centre Boom",          "sector": "Technology", "stat": "gdp_growth"},
    {"id": "09", "title": "NHS Dentistry Deserts",               "sector": "Healthcare", "stat": "cpih_inflation"},
    {"id": "10", "title": "The Eight A.M. GP Scramble",          "sector": "Healthcare", "stat": "economic_inactivity"},
    {"id": "11", "title": "The Social Care Workforce Gap",       "sector": "Healthcare", "stat": "vacancies"},
    {"id": "12", "title": "Too Ill to Work",                     "sector": "Healthcare", "stat": "economic_inactivity"},
    {"id": "13", "title": "The AI State School Divide",          "sector": "Education",  "stat": "unemployment"},
    {"id": "14", "title": "The Persistent Absence Generation",   "sector": "Education",  "stat": "economic_inactivity"},
    {"id": "15", "title": "The Teacher Retention Cliff",         "sector": "Education",  "stat": "vacancies"},
    {"id": "16", "title": "The SEND Funding Squeeze",            "sector": "Education",  "stat": "cpih_inflation"},
    {"id": "17", "title": "Generation Rent at Fifty",            "sector": "Housing",    "stat": "cpih_inflation"},
    {"id": "18", "title": "Trust in Westminster",                "sector": "Politics",   "stat": "cpih_inflation"},
    {"id": "19", "title": "Childhoods in Temporary Accommodation","sector": "Housing",   "stat": "unemployment"},
    {"id": "20", "title": "The Empty Homes Paradox",             "sector": "Housing",    "stat": "gdp_growth"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", stream=sys.stdout)
log = logging.getLogger("i20")


# --------------------------------------------------------------------------
# Step 1 — Fetch ONS CSV data (latest value + historical series)
# --------------------------------------------------------------------------

MONTH_NAMES = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

def parse_ons_csv(text):
    """Parse ONS CSV into list of (label, float_value) keeping all data rows."""
    reader = csv.reader(io.StringIO(text))
    rows = []
    for row in reader:
        if len(row) < 2:
            continue
        date_cell = row[0].strip()
        val_cell = row[1].strip()
        if not date_cell or not val_cell:
            continue
        try:
            val = float(val_cell)
        except ValueError:
            continue
        rows.append((date_cell, val))
    return rows


def fetch_ons_series(uri):
    """Download ONS CSV; return (latest_dict, historical_list) or (None, [])."""
    url = ONS_CSV.format(uri=uri)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS,
                                headers={"User-Agent": "i20.co.uk weekly-insights-bot"})
            resp.raise_for_status()
            rows = parse_ons_csv(resp.text)
            if not rows:
                log.warning("ONS CSV %s returned no usable rows", uri)
                return None, []
            # Filter to monthly rows for historical chart (contains 3-letter month)
            monthly = [(d, v) for d, v in rows
                       if any(m in d.upper() for m in MONTH_NAMES)]
            latest_date, latest_val = rows[-1]
            latest = {"value": str(latest_val), "period": latest_date}
            # Return last 60 monthly points for charting (5 years)
            return latest, monthly[-60:]
        except (requests.RequestException, ValueError) as exc:
            log.warning("ONS CSV %s attempt %d/%d: %s", uri, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None, []


def fetch_all_stats():
    """Fetch every stat; return {key: {latest, label, history}}."""
    stats = {}
    for key, cfg in STAT_SOURCES.items():
        latest, history = fetch_ons_series(cfg["uri"])
        if latest and latest["value"]:
            stats[key] = {"value": latest["value"], "period": latest["period"],
                          "label": cfg["label"], "history": history}
            log.info("Fetched %-22s = %s (%s), %d history points",
                     key, latest["value"], latest["period"], len(history))
        else:
            log.error("Could not fetch '%s'", key)
    return stats


# --------------------------------------------------------------------------
# Step 2 — Generate sector-coloured area charts
# --------------------------------------------------------------------------

def generate_chart(stat_key, stat_data, sector_color, out_path):
    """Create a filled area chart from historical monthly data."""
    history = stat_data.get("history", [])
    if len(history) < 6:
        log.warning("Insufficient data for chart %s (%d points)", stat_key, len(history))
        return False

    labels = [h[0] for h in history]
    values = [h[1] for h in history]

    # Thin out x-axis labels: show every 6th
    x_positions = list(range(len(labels)))
    display_labels = []
    display_ticks = []
    for i, lbl in enumerate(labels):
        if i % 6 == 0:
            # Shorten "2024 JAN" to "Jan 24"
            parts = lbl.split()
            if len(parts) == 2:
                short = parts[1].capitalize()[:3] + " " + parts[0][2:]
            else:
                short = lbl
            display_labels.append(short)
            display_ticks.append(i)

    fig, ax = plt.subplots(figsize=(7, 2.8), dpi=150)
    fig.patch.set_facecolor("#f0f1f3")
    ax.set_facecolor("#f0f1f3")

    ax.fill_between(x_positions, values, alpha=0.18, color=sector_color)
    ax.plot(x_positions, values, color=sector_color, linewidth=2.2)

    ax.set_xticks(display_ticks)
    ax.set_xticklabels(display_labels, fontsize=8, color="#52526a")
    ax.tick_params(axis="y", labelsize=8, colors="#52526a")
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#d0d1d8")
    ax.spines["bottom"].set_color("#d0d1d8")
    ax.grid(axis="y", color="#e2e3e8", linewidth=0.5)

    ax.set_title(stat_data["label"], fontsize=10, color="#1a1a2e",
                 fontweight="500", loc="left", pad=10)

    plt.tight_layout()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    log.info("Chart saved: %s", out_path)
    return True


def generate_all_charts(stats):
    """Generate one chart per stat key, saved to charts/ directory."""
    generated = set()
    for key, data in stats.items():
        # Pick sector colour from the first topic that uses this stat
        color = "#1e3a5f"
        for t in TOPICS:
            if t["stat"] == key:
                color = SECTOR_COLORS.get(t["sector"], color)
                break
        out = os.path.join(CHARTS_DIR, f"{key}.png")
        if generate_chart(key, data, color, out):
            generated.add(key)
    return generated


# --------------------------------------------------------------------------
# Step 3 — Generate ~500-word articles with gpt-4o-mini
# --------------------------------------------------------------------------

def build_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.critical("OPENAI_API_KEY not set. Aborting.")
        sys.exit(1)
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)


def generate_article(client, topic, stat):
    """Generate a ~500-word, four-paragraph analytical article."""
    user_prompt = (
        f"Weekly briefing topic: \"{topic['title']}\" "
        f"(sector: {topic['sector']}).\n"
        f"Latest official statistic — {stat['label']}: "
        f"{stat['value']} for {stat['period']} "
        f"(source: Office for National Statistics).\n\n"
        "Write the article now. Four paragraphs, ~500 words total. "
        "Respond with the article text only: no headings, no markdown, no quotation marks."
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL, temperature=0.7, max_tokens=900,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            if len(text.split()) >= 150:
                return text
            log.warning("Article too short for %s (attempt %d)", topic["id"], attempt)
        except Exception as exc:
            log.warning("OpenAI failed for %s attempt %d/%d: %s",
                        topic["id"], attempt, MAX_RETRIES, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


def article_to_html(text):
    """Convert plain-text article (double-newline separated) into <p> tags."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join(f"<p>{html.escape(p, quote=False)}</p>" for p in paragraphs)


def summarise_for_card(text, stat):
    """First sentence + latest figure for card teaser."""
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return f"{first} Latest figure: {stat['value']} ({stat['period']})."


# --------------------------------------------------------------------------
# Step 4 — Inject into index.html
# --------------------------------------------------------------------------

def inject_text(html_doc, element_id, new_text):
    """Replace inner text of a <p>/<span>/<strong> element by id."""
    pattern = re.compile(
        r'(<(?P<tag>p|span|strong)\b[^>]*\bid="' + re.escape(element_id) +
        r'"[^>]*>)(.*?)(</(?P=tag)>)', re.DOTALL)
    safe = html.escape(new_text, quote=False)
    updated, n = pattern.subn(lambda m: m.group(1) + safe + m.group(4), html_doc, count=1)
    if n == 0:
        log.error("Element '%s' not found — skipped", element_id)
        return html_doc
    return updated


def inject_html(html_doc, element_id, new_html):
    """Replace innerHTML of a <div> element by id (no escaping — caller provides safe HTML)."""
    pattern = re.compile(
        r'(<div\b[^>]*\bid="' + re.escape(element_id) +
        r'"[^>]*>)(.*?)(</div>)', re.DOTALL)
    updated, n = pattern.subn(lambda m: m.group(1) + new_html + m.group(3), html_doc, count=1)
    if n == 0:
        log.error("Div '%s' not found — skipped", element_id)
        return html_doc
    return updated


def save_atomically(path, content):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def main():
    log.info("=== i20 weekly update starting (v2) ===")

    if not os.path.exists(INDEX_FILE):
        log.critical("index.html not found at %s", INDEX_FILE)
        return 1

    stats = fetch_all_stats()
    if not stats:
        log.critical("No statistics fetched — leaving site untouched.")
        return 1

    # Generate charts
    chart_keys = generate_all_charts(stats)
    log.info("Charts generated for: %s", ", ".join(sorted(chart_keys)) or "none")

    # Generate articles
    client = build_client()
    with open(INDEX_FILE, "r", encoding="utf-8") as fh:
        html_doc = fh.read()

    updated = 0
    for topic in TOPICS:
        stat = stats.get(topic["stat"])
        if not stat:
            log.warning("Card %s: stat unavailable, keeping old text", topic["id"])
            continue

        article = generate_article(client, topic, stat)
        if not article:
            log.warning("Card %s: generation failed, keeping old text", topic["id"])
            continue

        article_html = article_to_html(article)
        html_doc = inject_html(html_doc, f"insight-{topic['id']}-content", article_html)
        html_doc = inject_text(html_doc, f"card-{topic['id']}-desc",
                               summarise_for_card(article, stat))
        updated += 1
        log.info("Card %s updated (%s)", topic["id"], topic["title"])

    if updated == 0:
        log.critical("Zero cards updated — refusing to rewrite index.html.")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    html_doc = inject_text(html_doc, "last-updated", stamp)
    save_atomically(INDEX_FILE, html_doc)

    log.info("=== Done: %d/%d cards refreshed ===", updated, len(TOPICS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
