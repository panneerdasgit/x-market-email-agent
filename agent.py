from __future__ import annotations

import argparse
import html
import json
import logging
import os
import smtplib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import feedparser
from bs4 import BeautifulSoup
from google import genai


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_FILE = BASE_DIR / "history.json"
PROMPTS_DIR = BASE_DIR / "prompts"
INDIA_TZ = ZoneInfo("Asia/Kolkata")

VALID_JOB_TYPES = {
    "premarket",
    "breaking",
    "stock_focus",
    "education",
    "closing",
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
    return " ".join(
        BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True).split()
    )


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
        "education_topics",
    ]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError("Missing config values: " + ", ".join(missing))

    if int(config["max_characters"]) > 280:
        raise ValueError("max_characters must not exceed 280 for a normal X post.")

    if int(config["max_characters"]) < 100:
        raise ValueError("max_characters is too small; use at least 100.")

    if not isinstance(config["rss_sources"], list) or not config["rss_sources"]:
        raise ValueError("At least one RSS source is required.")

    if not isinstance(config["education_topics"], list) or not config["education_topics"]:
        raise ValueError("At least one education topic is required.")


def load_prompt(
    job_type: str,
    config: dict[str, Any],
    education_topic: str = "",
) -> str:
    prompt_path = PROMPTS_DIR / f"{job_type}.txt"
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
    )


def fetch_news(
    config: dict[str, Any],
) -> list[dict[str, str]]:
    """Read the latest RSS items on every run.

    Version 2.1 intentionally allows the same day's news to be reused from
    different angles by different scheduled jobs.
    """
    items: list[dict[str, str]] = []

    for source in config["rss_sources"]:
        source_name = str(source.get("name", "Unknown")).strip()
        source_url = str(source.get("url", "")).strip()
        if not source_url:
            continue

        LOGGER.info("Reading RSS source: %s", source_name)
        feed = feedparser.parse(
            source_url,
            request_headers={
                "User-Agent": "Mozilla/5.0 (compatible; XMarketEmailAgent/2.1)"
            },
        )

        if getattr(feed, "bozo", False):
            LOGGER.warning(
                "RSS warning for %s: %s",
                source_name,
                getattr(feed, "bozo_exception", "Unknown warning"),
            )

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

    unique: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in items:
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        unique.append(item)

    return unique[: int(config["max_news_items"])]


def build_news_context(news_items: list[dict[str, str]]) -> str:
    sections: list[str] = []

    for number, item in enumerate(news_items, start=1):
        sections.append(
            "\n".join(
                [
                    f"NEWS {number}",
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
        actions = [str(action).lower().replace("_", "") for action in actions]

        if actions and "generatecontent" not in actions:
            continue

        models.append(name)

    return list(dict.fromkeys(models))


def choose_models(client: genai.Client, config: dict[str, Any]) -> list[str]:
    available = available_generation_models(client)
    if not available:
        raise RuntimeError("No Gemini text-generation model is available to this API key.")

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

    # Prefer Flash/Lite models returned by the API, without hard-coding a
    # model that may not be available to this account.
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


def generate_raw(
    client: genai.Client,
    model: str,
    prompt: str,
) -> str:
    response = client.models.generate_content(model=model, contents=prompt)
    return extract_text(response)


def enforce_character_limit(
    client: genai.Client,
    model: str,
    post: str,
    config: dict[str, Any],
) -> str:
    limit = int(config["max_characters"])
    post = " ".join(post.split())

    if len(post) <= limit:
        return post

    LOGGER.warning(
        "Generated post has %d characters; asking Gemini to shorten it.",
        len(post),
    )

    shorten_prompt = f"""
Shorten the X post below to a maximum of {limit} characters, including spaces,
the disclaimer and hashtags.

Keep the main fact and why it matters.
Use simple natural English.
Do not add new facts.
Keep no more than {config['hashtags_count']} hashtags.
End with: {config['disclaimer']}
Return only the shortened post.

POST:
{post}
""".strip()

    shortened = " ".join(generate_raw(client, model, shorten_prompt).split())

    if len(shortened) > limit:
        raise RuntimeError(
            f"Gemini still returned {len(shortened)} characters; limit is {limit}."
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
        else "No news is required for this educational post."
    )

    recent_records = history.get("sent_posts", [])[-int(config["recent_posts_for_prompt"]):]
    recent_lines: list[str] = []
    for record in recent_records:
        if isinstance(record, dict):
            recent_lines.append(
                f"- {record.get('job_type', 'unknown')}: {record.get('post', '')}"
            )
        else:
            recent_lines.append(f"- {record}")
    recent_text = "\n".join(recent_lines) or "None"

    complete_prompt = f"""
{prompt}

SUPPLIED NEWS:
{news_context}

RECENT POSTS TO AVOID REPEATING:
{recent_text}

Create a fresh post for the current job type.
Reusing the same day's news is allowed when the angle is different.
Do not repeat the same opening phrase, wording or topic angle from recent posts.
""".strip()

    errors: list[str] = []

    for model in candidates:
        try:
            LOGGER.info("Trying Gemini model: %s", model)
            post = generate_raw(client, model, complete_prompt)
            post = enforce_character_limit(client, model, post, config)
            return post, model
        except Exception as exc:
            LOGGER.warning("Model %s failed: %s", model, exc)
            errors.append(f"{model}: {exc}")

    raise RuntimeError("All available Gemini models failed:\n" + "\n".join(errors))


def send_email(
    config: dict[str, Any],
    job_type: str,
    post: str,
    model_used: str,
) -> None:
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

    if not gmail_user:
        raise RuntimeError("GMAIL_USER secret is missing.")
    if not gmail_password:
        raise RuntimeError("GMAIL_APP_PASSWORD secret is missing.")

    subject_names = {
        "premarket": "Pre-Market Outlook",
        "breaking": "Important Market Update",
        "stock_focus": "Stock in Focus",
        "education": "Learn the Market",
        "closing": "Market Closing Summary",
    }

    generated_at = datetime.now(INDIA_TZ)

    body = f"""
READY TO POST ON X

--------------------------------------------------

{post}

--------------------------------------------------

Character count: {len(post)}/{config['max_characters']}
Generated: {generated_at.strftime('%d %b %Y, %I:%M %p IST')}
Model: {model_used}

Review before publishing.
""".strip()

    message = EmailMessage()
    message["Subject"] = (
        f"{subject_names[job_type]} - {generated_at.strftime('%d %b %Y')}"
    )
    message["From"] = gmail_user
    message["To"] = str(config["to_email"]).strip()
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(message)

    LOGGER.info("Email sent successfully.")


def next_education_topic(
    config: dict[str, Any],
    history: dict[str, Any],
) -> str:
    topics = config["education_topics"]
    index = int(history.get("education_topic_index", 0)) % len(topics)
    history["education_topic_index"] = (index + 1) % len(topics)
    return str(topics[index])


def update_history(
    history: dict[str, Any],
    job_type: str,
    post: str,
    model_used: str,
) -> None:
    """Store structured post and email history."""
    now = datetime.now(INDIA_TZ)

    posts = history.setdefault("sent_posts", [])
    emails = history.setdefault("emails_sent", [])

    posts.append(
        {
            "date": now.strftime("%Y-%m-%d"),
            "job_type": job_type,
            "post": post,
            "character_count": len(post),
        }
    )

    emails.append(
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
    """On Friday's closing run, keep only the latest configured history."""
    now = datetime.now(INDIA_TZ)

    if job_type != "closing" or now.weekday() != 4:
        return

    limit = int(config["weekly_history_limit"])
    history["sent_posts"] = history.get("sent_posts", [])[-limit:]
    history["emails_sent"] = history.get("emails_sent", [])[-limit:]

    LOGGER.info(
        "Friday cleanup completed. Kept latest %d posts and email records.",
        limit,
    )

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_type", choices=sorted(VALID_JOB_TYPES))
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    try:
        config = load_json(CONFIG_FILE)
        validate_config(config)

        history = load_json(
            HISTORY_FILE,
            default={
                "sent_posts": [],
                "emails_sent": [],
                "education_topic_index": 0,
            },
        )

        LOGGER.info("Starting Version 2.1 job: %s", args.job_type)

        education_topic = ""
        news_items: list[dict[str, str]] = []

        if args.job_type == "education":
            education_topic = next_education_topic(config, history)
            LOGGER.info("Education topic: %s", education_topic)
        else:
            news_items = fetch_news(config)
            if not news_items:
                LOGGER.warning("No RSS news was returned; no email will be sent.")
                return 0
            LOGGER.info("Collected %d news items.", len(news_items))

        prompt = load_prompt(args.job_type, config, education_topic)
        post, model_used = generate_post(
            config,
            prompt,
            news_items,
            history,
        )

        LOGGER.info("Generated post (%d characters): %s", len(post), post)

        send_email(config, args.job_type, post, model_used)
        update_history(
            history=history,
            job_type=args.job_type,
            post=post,
            model_used=model_used,
        )

        weekly_cleanup(
            config=config,
            history=history,
            job_type=args.job_type,
        )

        save_json(HISTORY_FILE, history)

        LOGGER.info("Version 2.1 job completed successfully.")
        return 0

    except Exception as exc:
        LOGGER.exception("Market agent failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
