from __future__ import annotations

import argparse
import html
import json
import logging
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup
from google import genai

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_FILE = BASE_DIR / "history.json"
PROMPTS_DIR = BASE_DIR / "prompts"
INDIA_TZ = ZoneInfo("Asia/Kolkata")

PROMPT_FILES = {
    "weekday_premarket": "01_weekday_premarket.txt",
    "weekday_breaking": "02_weekday_breaking.txt",
    "weekday_stock_focus": "03_weekday_stock_focus.txt",
    "weekday_education": "04_weekday_education.txt",
    "weekday_closing": "05_weekday_closing.txt",
    "saturday_weekly_recap": "06_saturday_weekly_recap.txt",
    "saturday_investing_lesson": "07_saturday_investing_lesson.txt",
    "sunday_next_week_outlook": "08_sunday_next_week_outlook.txt",
    "sunday_investing_tip": "09_sunday_investing_tip.txt",
    "holiday_notice": "10_holiday_notice.txt",
    "holiday_company_spotlight": "11_holiday_company_spotlight.txt",
    "holiday_next_trading_day": "12_holiday_next_trading_day.txt",
}

NEWS_JOBS = {
    "weekday_premarket",
    "weekday_breaking",
    "weekday_stock_focus",
    "weekday_closing",
    "saturday_weekly_recap",
    "sunday_next_week_outlook",
    "holiday_company_spotlight",
    "holiday_next_trading_day",
}

EDUCATION_JOBS = {"weekday_education", "saturday_investing_lesson"}

SUBJECTS = {
    "weekday_premarket": "Pre-Market Outlook",
    "weekday_breaking": "Important Market Update",
    "weekday_stock_focus": "Stock in Focus",
    "weekday_education": "Learn the Market",
    "weekday_closing": "Market Closing Summary",
    "saturday_weekly_recap": "Saturday Weekly Recap",
    "saturday_investing_lesson": "Saturday Investing Lesson",
    "sunday_next_week_outlook": "Sunday Week Ahead",
    "sunday_investing_tip": "Sunday Investing Tip",
    "holiday_notice": "Market Holiday Notice",
    "holiday_company_spotlight": "Holiday Company Spotlight",
    "holiday_next_trading_day": "Next Trading-Day Watch",
}

HOLIDAY_REDIRECTS = {
    "weekday_premarket": "holiday_notice",
    "weekday_breaking": None,
    "weekday_stock_focus": "holiday_company_spotlight",
    "weekday_education": None,
    "weekday_closing": "holiday_next_trading_day",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOGGER = logging.getLogger("market-agent")


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"Missing required file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def clean_html(value: str) -> str:
    decoded = html.unescape(value or "")
    text = BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())


def validate_config(config: dict[str, Any]) -> None:
    required = [
        "to_email", "max_characters", "hashtags_count", "language",
        "tone", "writing_style", "disclaimer", "max_news_items",
        "recent_posts_for_prompt", "weekly_history_limit",
        "content_style", "rss_sources", "market_holidays",
        "education_topics", "sunday_tip_topics",
    ]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError("Missing config values: " + ", ".join(missing))

    if not 100 <= int(config["max_characters"]) <= 280:
        raise ValueError("max_characters must be between 100 and 280.")

    if not config["rss_sources"]:
        raise ValueError("At least one RSS source is required.")

    if not config["education_topics"] or not config["sunday_tip_topics"]:
        raise ValueError("Topic lists cannot be empty.")


def holiday_for_date(config: dict[str, Any], date_text: str) -> str | None:
    for holiday in config.get("market_holidays", []):
        if str(holiday.get("date", "")).strip() == date_text:
            return str(holiday.get("name", "Market holiday")).strip()
    return None


def resolve_job(
    requested_job: str,
    config: dict[str, Any],
    manual_run: bool,
) -> tuple[str | None, str]:
    holiday_name = holiday_for_date(
        config,
        datetime.now(INDIA_TZ).strftime("%Y-%m-%d"),
    ) or ""

    if manual_run or not holiday_name:
        return requested_job, holiday_name

    if requested_job not in HOLIDAY_REDIRECTS:
        return requested_job, holiday_name

    redirected = HOLIDAY_REDIRECTS[requested_job]
    if redirected is None:
        LOGGER.info("Skipping %s on market holiday %s.", requested_job, holiday_name)
        return None, holiday_name

    LOGGER.info("Redirecting %s to %s for %s.", requested_job, redirected, holiday_name)
    return redirected, holiday_name


def next_rotating_value(
    config: dict[str, Any],
    history: dict[str, Any],
    values_key: str,
    index_key: str,
) -> str:
    values = config[values_key]
    index = int(history.get(index_key, 0)) % len(values)
    history[index_key] = (index + 1) % len(values)
    return str(values[index])


def next_header(
    config: dict[str, Any],
    history: dict[str, Any],
    job_type: str,
) -> str:
    style = config["content_style"]
    if not style.get("rotate_headers", True):
        return ""

    choices = style.get("headers", {}).get(job_type, [])
    if not choices:
        return ""

    indexes = history.setdefault("header_indexes", {})
    index = int(indexes.get(job_type, 0)) % len(choices)
    indexes[job_type] = (index + 1) % len(choices)
    return str(choices[index])


def load_prompt(
    job_type: str,
    config: dict[str, Any],
    header: str,
    education_topic: str = "",
    sunday_tip_topic: str = "",
    holiday_name: str = "",
) -> str:
    prompt_path = PROMPTS_DIR / PROMPT_FILES[job_type]
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    style = config["content_style"]
    avoid_phrases = "\n".join(
        f"- {phrase}" for phrase in style.get("avoid_ai_phrases", [])
    )

    return prompt_path.read_text(encoding="utf-8").format(
        max_characters=config["max_characters"],
        hashtags_count=config["hashtags_count"],
        language=config["language"],
        tone=config["tone"],
        writing_style=config["writing_style"],
        disclaimer=config["disclaimer"],
        max_sentences=style.get("max_sentences", 4),
        avoid_phrases=avoid_phrases,
        header=header,
        education_topic=education_topic,
        sunday_tip_topic=sunday_tip_topic,
        holiday_name=holiday_name or "Market holiday",
    ) + f"\n\nSUPPLIED SHORT HEADER:\n{header or 'No header required'}\n"


def fetch_news(config: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    for source in config["rss_sources"]:
        name = str(source.get("name", "Unknown")).strip()
        url = str(source.get("url", "")).strip()
        if not url:
            continue

        LOGGER.info("Reading RSS source: %s", name)

        try:
            feed = feedparser.parse(
                url,
                request_headers={
                    "User-Agent": "Mozilla/5.0 (compatible; XMarketEmailAgent/2.3)"
                },
            )

            if getattr(feed, "bozo", False):
                LOGGER.warning(
                    "RSS warning for %s: %s",
                    name,
                    getattr(feed, "bozo_exception", "Unknown warning"),
                )

            count = 0
            for entry in getattr(feed, "entries", []):
                title = clean_html(str(entry.get("title", "")))
                summary = clean_html(
                    str(entry.get("summary") or entry.get("description") or "")
                )
                link = str(entry.get("link", "")).strip()
                published = str(
                    entry.get("published") or entry.get("updated") or ""
                ).strip()

                if not title or not link:
                    continue

                items.append({
                    "source": name,
                    "title": title,
                    "summary": summary[:1800],
                    "link": link,
                    "published": published,
                })
                count += 1

            LOGGER.info("Collected %d entries from %s.", count, name)

        except Exception as exc:
            LOGGER.exception("Failed to read %s: %s", name, exc)

    unique: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    for item in items:
        key = " ".join(item["title"].lower().split())
        if key in seen_titles:
            continue
        seen_titles.add(key)
        unique.append(item)

    if not unique:
        return []

    source_names = [str(s.get("name", "Unknown")).strip() for s in config["rss_sources"]]
    selected: list[dict[str, str]] = []
    used: set[int] = set()
    limit = int(config["max_news_items"])

    while len(selected) < limit:
        added = False
        for source_name in source_names:
            for index, item in enumerate(unique):
                if index in used or item["source"] != source_name:
                    continue
                selected.append(item)
                used.add(index)
                added = True
                break
            if len(selected) >= limit:
                break
        if not added:
            break

    return selected


def build_news_context(news_items: list[dict[str, str]]) -> str:
    return "\n\n".join(
        "\n".join([
            f"NEWS {index}",
            f"Source: {item['source']}",
            f"Title: {item['title']}",
            f"Published: {item['published'] or 'Not supplied'}",
            f"Summary: {item['summary'] or 'Not supplied'}",
        ])
        for index, item in enumerate(news_items, start=1)
    )


def normalize_model_name(name: str) -> str:
    name = name.strip()
    return name[7:] if name.startswith("models/") else name


def available_models(client: genai.Client) -> list[str]:
    result: list[str] = []

    for model in client.models.list():
        name = normalize_model_name(str(getattr(model, "name", "")))
        lowered = name.lower()

        if not name or "gemini" not in lowered:
            continue
        if any(token in lowered for token in ("embedding", "image", "tts", "live")):
            continue

        actions = (
            getattr(model, "supported_actions", None)
            or getattr(model, "supported_generation_methods", None)
            or []
        )
        normalized = [str(action).lower().replace("_", "") for action in actions]

        if normalized and "generatecontent" not in normalized:
            continue

        result.append(name)

    return list(dict.fromkeys(result))


def choose_models(client: genai.Client, config: dict[str, Any]) -> list[str]:
    available = available_models(client)
    if not available:
        raise RuntimeError("No Gemini text-generation model is available.")

    ordered: list[str] = []

    def add(name: str) -> None:
        if name in available and name not in ordered:
            ordered.append(name)

    for item in config.get("preferred_models", []):
        add(normalize_model_name(str(item)))
    for name in available:
        if "flash-lite" in name.lower():
            add(name)
    for name in available:
        if "flash" in name.lower():
            add(name)
    for name in available:
        add(name)

    LOGGER.info("Gemini candidates: %s", ", ".join(ordered))
    return ordered


def extract_text(response: Any) -> str:
    try:
        if isinstance(response.text, str) and response.text.strip():
            return response.text.strip()
    except Exception:
        pass

    parts: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())

    result = "\n".join(parts).strip()
    if not result:
        raise RuntimeError("Gemini returned no text.")
    return result


def generate_raw(client: genai.Client, model: str, prompt: str) -> str:
    return extract_text(
        client.models.generate_content(model=model, contents=prompt)
    )


def recent_posts_text(history: dict[str, Any], config: dict[str, Any]) -> str:
    records = history.get("sent_posts", [])
    selected = records[-int(config["recent_posts_for_prompt"]):]

    if not selected:
        return "None"

    return "\n".join(
        f"- {record.get('job_type', 'unknown')}: {record.get('post', '')}"
        if isinstance(record, dict)
        else f"- {record}"
        for record in selected
    )


def enforce_limit(
    client: genai.Client,
    model: str,
    post: str,
    config: dict[str, Any],
) -> str:
    limit = int(config["max_characters"])
    post = " ".join(post.split())

    if len(post) <= limit:
        return post

    shorten_prompt = f"""
Shorten this X post to at most {limit} characters including spaces, header,
disclaimer and hashtags.

Keep only the most useful fact and why it matters.
Use simple natural English.
Do not add facts.
Keep no more than {config['hashtags_count']} hashtags.
End with: {config['disclaimer']}
Return only the shortened post.

POST:
{post}
""".strip()

    shortened = " ".join(generate_raw(client, model, shorten_prompt).split())

    if len(shortened) > limit:
        raise RuntimeError(
            f"Post is still {len(shortened)} characters; limit is {limit}."
        )

    return shortened


def generate_post(
    config: dict[str, Any],
    prompt: str,
    news_items: list[dict[str, str]],
    history: dict[str, Any],
) -> tuple[str, str]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    client = genai.Client(api_key=api_key)
    candidates = choose_models(client, config)

    news_context = (
        build_news_context(news_items)
        if news_items
        else "No current news is required for this post."
    )

    complete_prompt = f"""
{prompt}

SUPPLIED NEWS:
{news_context}

RECENT POSTS TO AVOID REPEATING:
{recent_posts_text(history, config)}

Final instructions:
- Do not repeat recent openings or wording.
- Do not fill space unnecessarily.
- A clear post between 170 and 230 characters is preferred.
- Never exceed {config['max_characters']} characters.
""".strip()

    errors: list[str] = []

    for model in candidates:
        try:
            LOGGER.info("Trying Gemini model: %s", model)
            post = generate_raw(client, model, complete_prompt)
            return enforce_limit(client, model, post, config), model
        except Exception as exc:
            LOGGER.warning("Model %s failed: %s", model, exc)
            errors.append(f"{model}: {exc}")

    raise RuntimeError("All Gemini models failed:\n" + "\n".join(errors))


def send_email(
    config: dict[str, Any],
    job_type: str,
    post: str,
    model_used: str,
) -> None:
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

    if not gmail_user or not gmail_password:
        raise RuntimeError("Gmail secrets are missing.")

    now = datetime.now(INDIA_TZ)
    message = EmailMessage()
    message["Subject"] = f"{SUBJECTS[job_type]} - {now.strftime('%d %b %Y')}"
    message["From"] = gmail_user
    message["To"] = str(config["to_email"]).strip()
    message.set_content(
        f"""READY TO POST ON X

--------------------------------------------------

{post}

--------------------------------------------------

Character count: {len(post)}/{config['max_characters']}
Generated: {now.strftime('%d %b %Y, %I:%M %p IST')}
Content type: {job_type}
Model: {model_used}

Review before publishing."""
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(message)

    LOGGER.info("Email sent successfully.")


def update_history(
    history: dict[str, Any],
    job_type: str,
    post: str,
    model_used: str,
) -> None:
    now = datetime.now(INDIA_TZ)

    history.setdefault("sent_posts", []).append({
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "job_type": job_type,
        "post": post,
        "character_count": len(post),
    })

    history.setdefault("emails_sent", []).append({
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "job_type": job_type,
        "model": model_used,
    })


def weekly_cleanup(
    config: dict[str, Any],
    history: dict[str, Any],
    job_type: str,
) -> None:
    now = datetime.now(INDIA_TZ)

    if job_type != "weekday_closing" or now.weekday() != 4:
        return

    limit = int(config["weekly_history_limit"])
    history["sent_posts"] = history.get("sent_posts", [])[-limit:]
    history["emails_sent"] = history.get("emails_sent", [])[-limit:]
    LOGGER.info("Friday cleanup kept the latest %d records.", limit)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_type", choices=sorted(PROMPT_FILES))
    parser.add_argument("--manual", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    try:
        config = load_json(CONFIG_FILE)
        validate_config(config)

        history = load_json(HISTORY_FILE, default={
            "sent_posts": [],
            "emails_sent": [],
            "education_topic_index": 0,
            "sunday_tip_index": 0,
            "header_indexes": {},
        })

        job_type, holiday_name = resolve_job(args.job_type, config, args.manual)
        if job_type is None:
            return 0

        education_topic = ""
        sunday_tip_topic = ""
        news_items: list[dict[str, str]] = []

        if job_type in EDUCATION_JOBS:
            education_topic = next_rotating_value(
                config, history, "education_topics", "education_topic_index"
            )
        elif job_type == "sunday_investing_tip":
            sunday_tip_topic = next_rotating_value(
                config, history, "sunday_tip_topics", "sunday_tip_index"
            )
        elif job_type in NEWS_JOBS:
            news_items = fetch_news(config)
            if not news_items:
                LOGGER.warning("No RSS news was returned; no email will be sent.")
                return 0

        header = next_header(config, history, job_type)
        prompt = load_prompt(
            job_type=job_type,
            config=config,
            header=header,
            education_topic=education_topic,
            sunday_tip_topic=sunday_tip_topic,
            holiday_name=holiday_name,
        )

        post, model_used = generate_post(config, prompt, news_items, history)
        LOGGER.info("Generated post (%d characters): %s", len(post), post)

        send_email(config, job_type, post, model_used)
        update_history(history, job_type, post, model_used)
        weekly_cleanup(config, history, job_type)
        save_json(HISTORY_FILE, history)

        LOGGER.info("Version 2.3 completed successfully.")
        return 0

    except Exception as exc:
        LOGGER.exception("Market agent failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
