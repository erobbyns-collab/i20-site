#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_data.py — i20.co.uk weekly content pipeline.

Runs unattended every Monday via GitHub Actions:

    1. FETCH   — pulls the latest official figures from the ONS open API.
    2. WRITE   — sends each figure + topic to OpenAI's gpt-4o-mini, which
                 returns a ~100-word analytical paragraph in British English.
    3. INJECT  — rewrites the text inside `index.html` elements whose ids
                 match `insight-XX-content` and `card-XX-desc`.
    4. SAVE    — writes atomically (temp file + os.replace) so a crash can
                 never leave a half-written homepage on disk.

Design guarantees:
    * No API key is ever hardcoded — read from OPENAI_API_KEY env var.
    * Every network call has a timeout, retries and a graceful fallback.
    * If a stat cannot be fetched or a paragraph cannot be generated, the
      card simply keeps last week's text; the run never corrupts the site.
    * The HTML injector only rewrites single, non-nested <p> elements and
      escapes all generated text, so malformed model output cannot break
      the page markup.

Dependencies: requests, openai   (see .github/workflows/update_site.yml)
"""

from __future__ import annotations

import html
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests
from openai import OpenAI

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

OPENAI_MODEL = "gpt-4o-mini"          # fast + budget-friendly
OPENAI_TIMEOUT_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 20
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 4

# The strict systemic prompt required by the editorial spec.
SYSTEM_PROMPT = (
    "Act as an expert British societal sociologist. Write a concise, "
    "100-word authoritative analytical paragraph evaluating how this "
    "specific statistic impacts modern UK life, utilising British "
    "English spelling."
)

# ONS Beta API — open, keyless, JSON. Each entry is one national statistic.
#   dataset / series ids are stable ONS identifiers.
#   "freq" tells the parser which observation array to read.
ONS_BASE = "https://api.ons.gov.uk/timeseries/{series}/dataset/{dataset}/data"

STAT_SOURCES: dict[str, dict] = {
    "cpih_inflation": {
        "label": "CPIH annual inflation rate (%)",
        "dataset": "mm23", "series": "l55o", "freq": "months",
    },
    "unemployment": {
        "label": "UK unemployment rate, ages 16+ (%)",
        "dataset": "lms", "series": "mgsx", "freq": "months",
    },
    "employment_rate": {
        "label": "UK employment rate, ages 16-64 (%)",
        "dataset": "lms", "series": "lf24", "freq": "months",
    },
    "economic_inactivity": {
        "label": "UK economic inactivity rate, ages 16-64 (%)",
        "dataset": "lms", "series": "lf2s", "freq": "months",
    },
    "gdp_growth": {
        "label": "UK GDP quarter-on-quarter growth (%)",
        "dataset": "pn2", "series": "ihyq", "freq": "quarters",
    },
    "vacancies": {
        "label": "UK job vacancies, total (thousands)",
        "dataset": "unem", "series": "ap2y", "freq": "months",
    },
}

# Editorial map: which national statistic anchors each of the 20 cards.
# The mapping is deliberately explicit so an editor can retune it in one
# place, or extend STAT_SOURCES with new ONS series and point cards at them.
TOPICS: list[dict] = [
    {"id": "01", "title": "High Street Bank Deserts",            "sector": "Economy",    "stat": "gdp_growth"},
    {"id": "02", "title": "The Cash Acceptance Cliff",           "sector": "Economy",    "stat": "cpih_inflation"},
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
    {"id": "18", "title": "The Leasehold Trap",                  "sector": "Housing",    "stat": "cpih_inflation"},
    {"id": "19", "title": "Childhoods in Temporary Accommodation","sector": "Housing",   "stat": "unemployment"},
    {"id": "20", "title": "The Empty Homes Paradox",             "sector": "Housing",    "stat": "gdp_growth"},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("i20")


# --------------------------------------------------------------------------
# Step 1 — Fetch live statistics from the ONS open API
# --------------------------------------------------------------------------

def fetch_ons_series(dataset: str, series: str, freq: str) -> dict | None:
    """
    Return the latest observation for one ONS time series as
    {"value": "6.7", "period": "May 2026"} — or None on failure.
    Retries transient failures with linear backoff.
    """
    url = ONS_BASE.format(series=series, dataset=dataset)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=HTTP_TIMEOUT_SECONDS,
                headers={"User-Agent": "i20.co.uk weekly-insights-bot"},
            )
            resp.raise_for_status()
            payload = resp.json()
            observations = payload.get(freq) or []
            if not observations:
                log.warning("ONS %s/%s returned no '%s' observations", dataset, series, freq)
                return None
            latest = observations[-1]
            return {
                "value": str(latest.get("value", "")).strip(),
                "period": str(latest.get("date", "")).strip(),
            }
        except (requests.RequestException, ValueError) as exc:
            log.warning("ONS fetch %s/%s attempt %d/%d failed: %s",
                        dataset, series, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


def fetch_all_stats() -> dict[str, dict]:
    """Fetch every configured statistic once and cache it in memory."""
    stats: dict[str, dict] = {}
    for key, cfg in STAT_SOURCES.items():
        result = fetch_ons_series(cfg["dataset"], cfg["series"], cfg["freq"])
        if result and result["value"]:
            stats[key] = {**result, "label": cfg["label"]}
            log.info("Fetched %-22s = %s (%s)", key, result["value"], result["period"])
        else:
            log.error("Could not fetch statistic '%s' — dependent cards keep last week's text", key)
    return stats


# --------------------------------------------------------------------------
# Step 2 — Generate analytical paragraphs with gpt-4o-mini
# --------------------------------------------------------------------------

def build_client() -> OpenAI:
    """Create the OpenAI client from the environment — never hardcoded."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.critical("OPENAI_API_KEY is not set in the environment. Aborting.")
        sys.exit(1)
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)


def generate_paragraph(client: OpenAI, topic: dict, stat: dict) -> str | None:
    """
    Ask the model for one ~100-word analytical paragraph anchored to the
    live statistic. Returns None on failure so the card keeps old text.
    """
    user_prompt = (
        f"Weekly briefing topic: \"{topic['title']}\" "
        f"(sector: {topic['sector']}).\n"
        f"Latest official statistic — {stat['label']}: "
        f"{stat['value']} for {stat['period']} "
        f"(source: Office for National Statistics).\n\n"
        "Write the paragraph now. Respond with the paragraph text only: "
        "no headings, no quotation marks, no markdown."
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.7,
                max_tokens=260,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            # Basic sanity gate: refuse suspiciously short or empty output.
            if len(text.split()) >= 40:
                return " ".join(text.split())   # collapse stray newlines
            log.warning("Model output too short for card %s (attempt %d)",
                        topic["id"], attempt)
        except Exception as exc:  # network, rate-limit, auth, etc.
            log.warning("OpenAI call failed for card %s attempt %d/%d: %s",
                        topic["id"], attempt, MAX_RETRIES, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


def summarise_for_card(paragraph: str, stat: dict) -> str:
    """
    Derive the short card-face teaser locally (no extra API spend):
    first sentence of the analysis plus the anchoring figure.
    """
    first_sentence = re.split(r"(?<=[.!?])\s+", paragraph, maxsplit=1)[0]
    return f"{first_sentence} Latest figure: {stat['value']} ({stat['period']})."


# --------------------------------------------------------------------------
# Step 3 — Inject fresh text into index.html by element id
# --------------------------------------------------------------------------

def inject_text(html_doc: str, element_id: str, new_text: str) -> str:
    """
    Replace the inner text of the single <p> (or <span>/<strong>) element
    carrying `element_id`. The pattern requires the element to contain no
    nested block tags — which the index.html template guarantees — so a
    non-greedy match to the closing tag is safe. Generated text is
    HTML-escaped, making markup breakage from model output impossible.
    """
    pattern = re.compile(
        r'(<(?P<tag>p|span|strong)\b[^>]*\bid="' + re.escape(element_id) +
        r'"[^>]*>)(.*?)(</(?P=tag)>)',
        re.DOTALL,
    )
    safe_text = html.escape(new_text, quote=False)
    updated, count = pattern.subn(
        lambda m: m.group(1) + safe_text + m.group(4), html_doc, count=1
    )
    if count == 0:
        log.error("Element id '%s' not found in index.html — skipped", element_id)
        return html_doc
    return updated


def save_atomically(path: str, content: str) -> None:
    """Write via a temp file in the same directory, then os.replace()."""
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
        os.replace(tmp_path, path)   # atomic on POSIX
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def main() -> int:
    log.info("=== i20 weekly update starting ===")

    if not os.path.exists(INDEX_FILE):
        log.critical("index.html not found at %s", INDEX_FILE)
        return 1

    stats = fetch_all_stats()
    if not stats:
        log.critical("No statistics could be fetched — leaving site untouched.")
        return 1

    client = build_client()

    with open(INDEX_FILE, "r", encoding="utf-8") as fh:
        html_doc = fh.read()

    updated_cards = 0
    for topic in TOPICS:
        stat = stats.get(topic["stat"])
        if not stat:
            log.warning("Card %s (%s): statistic unavailable, keeping old text",
                        topic["id"], topic["title"])
            continue

        paragraph = generate_paragraph(client, topic, stat)
        if not paragraph:
            log.warning("Card %s (%s): generation failed, keeping old text",
                        topic["id"], topic["title"])
            continue

        html_doc = inject_text(html_doc, f"insight-{topic['id']}-content", paragraph)
        html_doc = inject_text(html_doc, f"card-{topic['id']}-desc",
                               summarise_for_card(paragraph, stat))
        updated_cards += 1
        log.info("Card %s updated (%s)", topic["id"], topic["title"])

    if updated_cards == 0:
        log.critical("Zero cards updated — refusing to rewrite index.html.")
        return 1

    # Stamp the masthead with the refresh date, then save safely.
    stamp = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    html_doc = inject_text(html_doc, "last-updated", stamp)
    save_atomically(INDEX_FILE, html_doc)

    log.info("=== Done: %d/%d cards refreshed, index.html saved ===",
             updated_cards, len(TOPICS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
