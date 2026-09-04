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


# URL slugs for individual article pages
SLUGS = {
    "01": "high-street-bank-deserts",
    "02": "the-turnout-crisis",
    "03": "regional-productivity-divide",
    "04": "gig-economy-pension-void",
    "05": "rural-broadband-last-mile",
    "06": "digital-only-by-default",
    "07": "ai-and-the-entry-level-job",
    "08": "britains-data-centre-boom",
    "09": "nhs-dentistry-deserts",
    "10": "the-eight-am-gp-scramble",
    "11": "social-care-workforce-gap",
    "12": "too-ill-to-work",
    "13": "ai-state-school-divide",
    "14": "persistent-absence-generation",
    "15": "teacher-retention-cliff",
    "16": "send-funding-squeeze",
    "17": "generation-rent-at-fifty",
    "18": "trust-in-westminster",
    "19": "childhoods-in-temporary-accommodation",
    "20": "empty-homes-paradox",
}

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



# --------------------------------------------------------------------------
# Step 5 — Generate individual article pages for SEO
# --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en-GB">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — i20</title>
  <meta name="description" content="{meta_desc}">
  <meta property="og:type" content="article">
  <meta property="og:url" content="https://i20.co.uk/insights/{slug}.html">
  <meta property="og:title" content="{title} — i20">
  <meta property="og:description" content="{meta_desc}">
  <meta property="og:image" content="https://i20.co.uk/charts/{stat_key}.png">
  <meta property="og:locale" content="en_GB">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="canonical" href="https://i20.co.uk/insights/{slug}.html">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,500;9..144,700&family=Newsreader:opsz,wght@6..72,400;6..72,500&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <!-- Google Analytics -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-KFDVTZ9X7M"></script>
  <script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments);}}gtag('js',new Date());gtag('config','G-KFDVTZ9X7M');</script>
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "NewsArticle",
    "headline": "{title}",
    "datePublished": "{date_iso}",
    "dateModified": "{date_iso}",
    "author": {{"@type": "Organization", "name": "i20"}},
    "publisher": {{"@type": "Organization", "name": "i20", "url": "https://i20.co.uk"}},
    "description": "{meta_desc}",
    "mainEntityOfPage": "https://i20.co.uk/insights/{slug}.html",
    "image": "https://i20.co.uk/charts/{stat_key}.png"
  }}
  </script>
  <style>
    :root{{--bg-page:#f0f1f3;--bg-card:#fff;--text-primary:#1a1a2e;--text-secondary:#52526a;--text-muted:#8a8a9e;--border-light:#e2e3e8;--border-rule:#d0d1d8;--sector-economy:#1e3a5f;--sector-technology:#0d9488;--sector-healthcare:#e8634a;--sector-education:#d97706;--sector-housing:#2d8a56;--sector-politics:#6C3483;--font-display:"Fraunces",Georgia,serif;--font-body:"Newsreader",Georgia,serif;--font-ui:"Inter",-apple-system,"Segoe UI",sans-serif}}
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:var(--bg-page);color:var(--text-primary);font-family:var(--font-ui);font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}}
    .page-wrap{{max-width:740px;margin:0 auto;padding:2rem 1.5rem 4rem}}
    .back-link{{display:inline-block;font-size:.8rem;font-weight:600;letter-spacing:.06em;color:var(--text-muted);text-decoration:none;margin-bottom:2rem;transition:color .2s}}
    .back-link:hover{{color:var(--text-primary)}}
    .article-card{{background:var(--bg-card);border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04)}}
    .article-stripe{{height:5px}}
    .article-header{{padding:2rem 2.5rem 1.5rem}}
    .article-sector{{font-size:.68rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase;margin-bottom:.75rem}}
    .article-title{{font-family:var(--font-display);font-weight:700;font-size:clamp(1.6rem,4vw,2.4rem);line-height:1.15;letter-spacing:-.02em}}
    .article-date{{font-size:.75rem;color:var(--text-muted);margin-top:.75rem}}
    .article-chart{{padding:1.5rem 2.5rem;background:var(--bg-page);border-top:1px solid var(--border-light);border-bottom:1px solid var(--border-light)}}
    .article-chart img{{width:100%;height:auto;border-radius:8px}}
    .article-body{{padding:2rem 2.5rem 2.5rem;font-family:var(--font-body);font-size:1.12rem;line-height:1.8}}
    .article-body p{{margin-bottom:1rem}}
    .article-body p:last-child{{margin-bottom:0}}
    .article-body p:first-of-type::first-letter{{font-family:var(--font-display);font-weight:700;font-size:3.2em;line-height:.8;float:left;padding:.06em .1em 0 0}}
    .article-source{{padding:1.25rem 2.5rem;border-top:1px solid var(--border-light);font-size:.72rem;color:var(--text-muted)}}
    .site-footer{{max-width:740px;margin:0 auto;padding:1.5rem 1.5rem 3rem;font-size:.75rem;color:var(--text-muted);display:flex;gap:.5rem;flex-wrap:wrap}}
    @media(max-width:600px){{.article-header,.article-body,.article-chart,.article-source{{padding-left:1.25rem;padding-right:1.25rem}}}}
  </style>
</head>
<body>
  <div class="page-wrap">
    <a href="https://i20.co.uk" class="back-link">&larr; Back to all 20 insights</a>
    <article class="article-card">
      <div class="article-stripe" style="background:{sector_color}"></div>
      <div class="article-header">
        <div class="article-sector" style="color:{sector_color}">{sector}</div>
        <h1 class="article-title">{title}</h1>
        <p class="article-date">Data refreshed {date_display}</p>
      </div>
      <div class="article-chart">
        <img src="../charts/{stat_key}.png" alt="{stat_label} trend chart" onerror="this.parentElement.style.display='none'">
      </div>
      <div class="article-body">
        {article_html}
      </div>
      <div class="article-source">i20 &middot; Analysis generated weekly from open UK government statistics via the Office for National Statistics.</div>
    </article>
  </div>
  <footer class="site-footer">
    <span>&copy; {year} i20.co.uk</span>
    <span>&middot;</span>
    <a href="mailto:info@i20.co.uk" style="color:var(--text-secondary);text-decoration:none">info@i20.co.uk</a>
  </footer>
</body>
</html>"""


def generate_article_page(topic, stat, article_html_content, stamp, date_iso):
    """Generate a standalone HTML page for one insight."""
    slug = SLUGS.get(topic["id"], "")
    if not slug:
        return None

    sector_color = SECTOR_COLORS.get(topic["sector"], "#1e3a5f")
    stat_label = stat.get("label", "")

    # Build meta description from first ~150 chars of article text
    import re as _re
    plain = _re.sub(r'<[^>]+>', '', article_html_content)
    meta_desc = plain[:155].rsplit(' ', 1)[0] + "..."

    page_html = PAGE_TEMPLATE.format(
        title=topic["title"],
        slug=slug,
        sector=topic["sector"],
        sector_color=sector_color,
        stat_key=topic["stat"],
        stat_label=stat_label,
        meta_desc=html.escape(meta_desc, quote=True),
        article_html=article_html_content,
        date_display=stamp,
        date_iso=date_iso,
        year=datetime.now(timezone.utc).strftime("%Y"),
    )

    insights_dir = os.path.join(os.path.dirname(INDEX_FILE), "insights")
    os.makedirs(insights_dir, exist_ok=True)
    page_path = os.path.join(insights_dir, f"{slug}.html")

    with open(page_path, "w", encoding="utf-8") as f:
        f.write(page_html)
    log.info("Page generated: insights/%s.html", slug)
    return slug


# --------------------------------------------------------------------------
# Step 6 — Generate "This Week's Lead" editorial
# --------------------------------------------------------------------------

EDITORIAL_PROMPT = (
    "You are the editor of i20, a weekly UK data publication. Based on the "
    "statistics provided, write a 3-4 sentence editorial introduction picking "
    "the single most striking or newsworthy data point this week. Be opinionated, "
    "direct, and conversational — this is your voice, not a report. Use British "
    "English. Write in first person plural ('we'). Do not use headings or markdown. "
    "Respond with the text only."
)


def generate_editorial(client, stats):
    """Generate the weekly lead editorial paragraph."""
    stats_summary = "\n".join(
        f"- {v['label']}: {v['value']} ({v['period']})" for v in stats.values()
    )
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL, temperature=0.8, max_tokens=200,
            messages=[
                {"role": "system", "content": EDITORIAL_PROMPT},
                {"role": "user", "content": f"This week's UK statistics:\n{stats_summary}"},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        if len(text.split()) >= 20:
            return text
    except Exception as exc:
        log.warning("Editorial generation failed: %s", exc)
    return None


# --------------------------------------------------------------------------
# Step 7 — Generate dynamic sitemap
# --------------------------------------------------------------------------

def generate_sitemap(updated_slugs, stamp_iso):
    """Write sitemap.xml with homepage + all individual article pages."""
    sitemap_path = os.path.join(os.path.dirname(INDEX_FILE), "sitemap.xml")
    urls = ['  <url>\n    <loc>https://i20.co.uk/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n    <lastmod>{}</lastmod>\n  </url>'.format(stamp_iso)]
    for slug in sorted(SLUGS.values()):
        urls.append('  <url>\n    <loc>https://i20.co.uk/insights/{}.html</loc>\n    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n    <lastmod>{}</lastmod>\n  </url>'.format(slug, stamp_iso))
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{}\n</urlset>\n'.format('\n'.join(urls))
    with open(sitemap_path, "w", encoding="utf-8") as f:
        f.write(sitemap)
    log.info("Sitemap generated with %d URLs", len(urls))

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
    stamp_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    html_doc = inject_text(html_doc, "last-updated", stamp)

    # Generate "This Week's Lead" editorial
    editorial = generate_editorial(client, stats)
    if editorial:
        html_doc = inject_text(html_doc, "lead-editorial-content", editorial)
        log.info("Editorial lead generated")

    save_atomically(INDEX_FILE, html_doc)

    # Generate individual article pages for SEO
    page_slugs = []
    for topic in TOPICS:
        stat = stats.get(topic["stat"])
        if not stat:
            continue
        content_el_id = f"insight-{topic['id']}-content"
        # Extract the article HTML from the updated index.html
        import re as _re
        match = _re.search(
            r'<div[^>]*\bid="' + _re.escape(content_el_id) + r'"[^>]*>(.*?)</div>',
            html_doc, _re.DOTALL
        )
        if match:
            article_html_content = match.group(1)
            slug = generate_article_page(topic, stat, article_html_content, stamp, stamp_iso)
            if slug:
                page_slugs.append(slug)

    # Generate dynamic sitemap
    generate_sitemap(page_slugs, stamp_iso[:10])

    log.info("=== Done: %d/%d cards refreshed, %d pages generated ===",
             updated, len(TOPICS), len(page_slugs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
