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
    "sunday_market_question": "09_sunday_market_question.txt",
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

SUBJECT_NAMES = {
    "weekday_premarket": "Pre-Market Outlook",
    "weekday_breaking": "Important Market Update",
    "weekday_stock_focus": "Stock in Focus",
    "weekday_education": "Learn the Market",
    "weekday_closing": "Market Closing Summary",
    "saturday_weekly_recap": "Saturday Weekly Recap",
    "saturday_investing_lesson": "Saturday Investing Lesson",
    "sunday_next_week_outlook": "Sunday Next-Week Outlook",
    "sunday_market_question": "Sunday Market Question",
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
        "to_email",
        "max_characters",
        "hashtags_count",
        "language",
        "tone",
        "writing_style",
        "disclaimer",
        "max_news_items",
        "recent_posts_for_prompt",
        "weekly_history_limit",
        "rss_sources",
        "market_holidays",
        "education_topics",
        "engagement_topics",
    ]

    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError("Missing config values: " + ", ".join(missing))

    if not 100 <= int(config["max_characters"]) <= 280:
        raise ValueError("max_characters must be between 100 and 280.")

    if not isinstance(config["rss_sources"], list) or not config["rss_sources"]:
        raise ValueError("At least one RSS source is required.")

    if not isinstance(config["education_topics"], list) or not config["education_topics"]:
        raise ValueError("At least one education topic is required.")

    if not isinstance(config["engagement_topics"], list) or not config["engagement_topics"]:
        raise ValueError("At least one engagement topic is required.")


def holiday_for_date(config: dict[str, Any], date_text: str) -> str | None:
    for holiday in config.get("market_holidays", []):
        if str(holiday.get("date", "")).strip() == date_text:
            return str(holiday.get("name", "Market holiday")).strip()
    return None


def resolve_automatic_job(
    requested_job: str,
    config: dict[str, Any],
    manual_run: bool,
) -> tuple[str | None, str]:
    today = datetime.now(INDIA_TZ).strftime("%Y-%m-%d")
    holiday_name = holiday_for_date(config, today) or ""

    if manual_run or not holiday_name:
        return requested_job, holiday_name

    if requested_job not in HOLIDAY_REDIRECTS:
        return requested_job, holiday_name

    redirected = HOLIDAY_REDIRECTS[requested_job]

    if redirected is None:
        LOGGER.info(
            "Skipping %s because %s is a configured market holiday.",
            requested_job,
            holiday_name,
        )
        return None, holiday_name

    LOGGER.info(
        "Market holiday detected (%s). Redirecting %s to %s.",
        holiday_name,
        requested_job,
        redirected,
    )
    return redirected, holiday_name


def load_prompt(
    job_type: str,
    config: dict[str, Any],
    education_topic: str = "",
    engagement_topic: str = "",
    holiday_name: str = "",
) -> str:
    prompt_path = PROMPTS_DIR / PROMPT_FILES[job_type]

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    return prompt_path.read_text(encoding="utf-8").format(
        max_characters=config["max_characters"],
        hashtags_count=config["hashtags_count"],
        language=config["language"],
        tone=config["tone"],
        writing_style=config["writing_style"],
        disclaimer=config["disclaimer"],
        education_topic=education_topic,
        engagement_topic=engagement_topic,
        holiday_name=holiday_name or "Market holiday",
    )


def fetch_news(config: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    for source in config["rss_sources"]:
        source_name = str(source.get("name", "Unknown")).strip()
        source_url = str(source.get("url", "")).strip()

        if not source_url:
            LOGGER.warning("Skipping RSS source without URL: %s", source_name)
            continue

        LOGGER.info("Reading RSS source: %s", source_name)

        try:
            feed = feedparser.parse(
                source_url,
                request_headers={
                    "User-Agent": "Mozilla/5.0 (compatible; XMarketEmailAgent/2.2)"
                },
            )

            if getattr(feed, "bozo", False):
                LOGGER.warning(
                    "RSS parsing warning for %s: %s",
                    source_name,
                    getattr(feed, "bozo_exception", "Unknown warning"),
                )

            source_count = 0

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

                items.append(
                    {
                        "source": source_name,
                        "title": title,
                        "summary": summary[:1800],
                        "link": link,
                        "published": published,
                    }
                )
                source_count += 1

            LOGGER.info("Collected %d entries from %s.", source_count, source_name)

        except Exception as exc:
            LOGGER.exception("Failed to read %s: %s", source_name, exc)

    unique: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    for item in items:
        title_key = " ".join(item["title"].lower().split())
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        unique.append(item)

    if not unique:
        return []

    max_items = int(config["max_news_items"])
    source_names = [
        str(source.get("name", "Unknown")).strip()
        for source in config["rss_sources"]
    ]

    selected: list[dict[str, str]] = []
    selected_indexes: set[int] = set()

    while len(selected) < max_items:
        added = False

        for source_name in source_names:
            for index, item in enumerate(unique):
                if index in selected_indexes or item["source"] != source_name:
                    continue

                selected.append(item)
                selected_indexes.add(index)
                added = True
                break

            if len(selected) >= max_items:
                break

        if not added:
            break

    return selected


def build_news_context(news_items: list[dict[str, str]]) -> str:
    sections: list[str] = []

    for number, item in enumerate(news_items, start=1):
        sections.append(
            "\n".join(
                [
                    f"NEWS {number}",
                    f"Source: {item['source']}",
                    f"Title: {item['title']}",
                    f"Published: {item['published'] or 'Not supplied'}",
                    f"Summary: {item['summary'] or 'Not supplied'}",
                ]
            )
        )

    return "\n\n".join(sections)


def normalize_model_name(name: str) -> str:
    name = name.strip()
    return name[7:] if name.startswith("models/") else name


def available_generation_models(client: genai.Client) -> list[str]:
    models: list[str] = []

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
        normalized_actions = [
            str(action).lower().replace("_", "")
            for action in actions
        ]

        if normalized_actions and "generatecontent" not in normalized_actions:
            continue

        models.append(name)

    return list(dict.fromkeys(models))


def choose_models(client: genai.Client, config: dict[str, Any]) -> list[str]:
    available = available_generation_models(client)

    if not available:
        raise RuntimeError(
            "No Gemini text-generation model is available to this API key."
        )

    preferred = [
        normalize_model_name(str(item))
        for item in config.get("preferred_models", [])
        if str(item).strip()
    ]

    ordered: list[str] = []

    def add(name: str) -> None:
        if name in available and name not in ordered:
            ordered.append(name)

    for name in preferred:
        add(name)

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
        value = response.text
        if isinstance(value, str) and value.strip():
            return value.strip()
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
    response = client.models.generate_content(model=model, contents=prompt)
    return extract_text(response)


def enforce_character_limit(
    client: genai.Client,
    model: str,
    post: str,
    config: dict[str, Any],
    job_type: str,
) -> str:
    limit = int(config["max_characters"])
    post = " ".join(post.split())

    if len(post) <= limit:
        return post

    disclaimer_rule = (
        "Do not add a disclaimer unless it naturally fits."
        if job_type == "sunday_market_question"
        else f"End with: {config['disclaimer']}"
    )

    shorten_prompt = f"""
Shorten the X post below to at most {limit} characters including spaces and hashtags.

Keep the main value.
Use simple natural English.
Do not add facts.
Use no more than {config['hashtags_count']} hashtags.
{disclaimer_rule}
Return only the shortened post.

POST:
{post}
""".strip()

    shortened = " ".join(generate_raw(client, model, shorten_prompt).split())

    if len(shortened) > limit:
        raise RuntimeError(
            f"Gemini returned {len(shortened)} characters after shortening; "
            f"the configured limit is {limit}."
        )

    return shortened


def recent_post_context(
    history: dict[str, Any],
    config: dict[str, Any],
) -> str:
    records = history.get("sent_posts", [])
    selected = records[-int(config["recent_posts_for_prompt"]):]

    if not selected:
        return "None"

    lines: list[str] = []

    for record in selected:
        if isinstance(record, dict):
            lines.append(
                f"- {record.get('job_type', 'unknown')}: "
                f"{record.get('post', '')}"
            )
        else:
            lines.append(f"- {record}")

    return "\n".join(lines)


def generate_post(
    config: dict[str, Any],
    prompt: str,
    news_items: list[dict[str, str]],
    history: dict[str, Any],
    job_type: str,
) -> tuple[str, str]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    client = genai.Client(api_key=api_key)
    candidates = choose_models(client, config)

    news_context = (
        build_news_context(news_items)
        if news_items
        else "No news is required for this post."
    )

    complete_prompt = f"""
{prompt}

SUPPLIED NEWS:
{news_context}

RECENT POSTS TO AVOID REPEATING:
{recent_post_context(history, config)}

Important:
- Create a fresh post for this content type.
- Do not repeat recent opening phrases, angles or wording.
- The same news may be reused only when this job needs a different angle.
""".strip()

    errors: list[str] = []

    for model in candidates:
        try:
            LOGGER.info("Trying Gemini model: %s", model)
            post = generate_raw(client, model, complete_prompt)
            post = enforce_character_limit(
                client,
                model,
                post,
                config,
                job_type,
            )
            return post, model
        except Exception as exc:
            LOGGER.warning("Model %s failed: %s", model, exc)
            errors.append(f"{model}: {exc}")

    raise RuntimeError(
        "All available Gemini models failed:\n" + "\n".join(errors)
    )


def send_email(
    config: dict[str, Any],
    job_type: str,
    post: str,
    model_used: str,
) -> None:
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_password = (
        os.getenv("GMAIL_APP_PASSWORD", "")
        .replace(" ", "")
        .strip()
    )

    if not gmail_user:
        raise RuntimeError("GMAIL_USER secret is missing.")

    if not gmail_password:
        raise RuntimeError("GMAIL_APP_PASSWORD secret is missing.")

    now = datetime.now(INDIA_TZ)

    body = f"""
READY TO POST ON X

--------------------------------------------------

{post}

--------------------------------------------------

Character count: {len(post)}/{config['max_characters']}
Generated: {now.strftime('%d %b %Y, %I:%M %p IST')}
Content type: {job_type}
Model: {model_used}

Review before publishing.
""".strip()

    message = EmailMessage()
    message["Subject"] = (
        f"{SUBJECT_NAMES[job_type]} - {now.strftime('%d %b %Y')}"
    )
    message["From"] = gmail_user
    message["To"] = str(config["to_email"]).strip()
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(message)

    LOGGER.info("Email sent successfully.")


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


def update_history(
    history: dict[str, Any],
    job_type: str,
    post: str,
    model_used: str,
) -> None:
    now = datetime.now(INDIA_TZ)

    history.setdefault("sent_posts", []).append(
        {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "job_type": job_type,
            "post": post,
            "character_count": len(post),
        }
    )

    history.setdefault("emails_sent", []).append(
        {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "job_type": job_type,
            "model": model_used,
        }
    )


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

    LOGGER.info(
        "Friday cleanup completed. Kept latest %d history records.",
        limit,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and email an Indian market X post."
    )
    parser.add_argument("job_type", choices=sorted(PROMPT_FILES))
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Use the selected prompt directly without holiday redirection.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    try:
        config = load_json(CONFIG_FILE)
        validate_config(config)

        history = load_json(
            HISTORY_FILE,
            default={
                "sent_posts": [],
                "emails_sent": [],
                "education_topic_index": 0,
                "engagement_topic_index": 0,
            },
        )

        resolved_job, holiday_name = resolve_automatic_job(
            arguments.job_type,
            config,
            arguments.manual,
        )

        if resolved_job is None:
            LOGGER.info("No post is scheduled for this holiday time slot.")
            return 0

        LOGGER.info(
            "Starting Version 2.2 job: requested=%s resolved=%s",
            arguments.job_type,
            resolved_job,
        )

        education_topic = ""
        engagement_topic = ""
        news_items: list[dict[str, str]] = []

        if resolved_job in EDUCATION_JOBS:
            education_topic = next_rotating_value(
                config,
                history,
                "education_topics",
                "education_topic_index",
            )
            LOGGER.info("Education topic: %s", education_topic)

        elif resolved_job == "sunday_market_question":
            engagement_topic = next_rotating_value(
                config,
                history,
                "engagement_topics",
                "engagement_topic_index",
            )
            LOGGER.info("Engagement topic: %s", engagement_topic)

        elif resolved_job in NEWS_JOBS:
            news_items = fetch_news(config)

            if not news_items:
                LOGGER.warning(
                    "No RSS news was returned from any source; no email will be sent."
                )
                return 0

            LOGGER.info("Selected %d news items.", len(news_items))

        prompt = load_prompt(
            resolved_job,
            config,
            education_topic=education_topic,
            engagement_topic=engagement_topic,
            holiday_name=holiday_name,
        )

        post, model_used = generate_post(
            config,
            prompt,
            news_items,
            history,
            resolved_job,
        )

        LOGGER.info("Generated post (%d characters): %s", len(post), post)

        send_email(config, resolved_job, post, model_used)
        update_history(history, resolved_job, post, model_used)
        weekly_cleanup(config, history, resolved_job)
        save_json(HISTORY_FILE, history)

        LOGGER.info("Version 2.2 job completed successfully.")
        return 0

    except Exception as exc:
        LOGGER.exception("Market agent failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
