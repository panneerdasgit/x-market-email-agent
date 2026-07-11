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

import feedparser
from bs4 import BeautifulSoup
from google import genai


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_FILE = BASE_DIR / "history.json"
PROMPTS_DIR = BASE_DIR / "prompts"

VALID_JOB_TYPES = {
    "premarket",
    "breaking",
    "midday",
    "sector",
    "closing",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOGGER = logging.getLogger("market-agent")


def load_json(path: Path) -> dict[str, Any]:
    """Load and return a JSON file."""

    if not path.exists():
        raise FileNotFoundError(f"Required file does not exist: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: Path, data: dict[str, Any]) -> None:
    """Write data to a JSON file."""

    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def clean_html(value: str) -> str:
    """Convert RSS HTML content into clean plain text."""

    decoded = html.unescape(value or "")
    soup = BeautifulSoup(decoded, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def validate_configuration(config: dict[str, Any]) -> None:
    """Validate required configuration values."""

    required_keys = [
        "to_email",
        "word_count",
        "hashtags_count",
        "language",
        "tone",
        "model",
        "max_news_items",
        "rss_sources",
    ]

    missing = [key for key in required_keys if key not in config]

    if missing:
        raise ValueError(
            "Missing required config values: " + ", ".join(missing)
        )

    if not isinstance(config["rss_sources"], list):
        raise ValueError("rss_sources must be a list.")

    if not config["rss_sources"]:
        raise ValueError("At least one RSS source is required.")


def load_prompt(job_type: str, config: dict[str, Any]) -> str:
    """Read and format the correct prompt file."""

    prompt_file = PROMPTS_DIR / f"{job_type}.txt"

    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

    template = prompt_file.read_text(encoding="utf-8")

    return template.format(
        word_count=config["word_count"],
        hashtags_count=config["hashtags_count"],
        language=config["language"],
        tone=config["tone"],
    )


def fetch_news(
    config: dict[str, Any],
    processed_links: set[str],
) -> list[dict[str, str]]:
    """Read configured RSS feeds and return unused news items."""

    all_items: list[dict[str, str]] = []

    for source in config["rss_sources"]:
        source_name = source.get("name", "Unknown source")
        source_url = source.get("url", "").strip()

        if not source_url:
            LOGGER.warning("Skipping RSS source without URL: %s", source_name)
            continue

        LOGGER.info("Reading RSS source: %s", source_name)

        try:
            feed = feedparser.parse(source_url)

            if feed.bozo:
                LOGGER.warning(
                    "RSS warning for %s: %s",
                    source_name,
                    feed.bozo_exception,
                )

            for entry in feed.entries:
                title = clean_html(entry.get("title", ""))
                summary = clean_html(
                    entry.get("summary")
                    or entry.get("description")
                    or ""
                )
                link = entry.get("link", "").strip()
                published = (
                    entry.get("published")
                    or entry.get("updated")
                    or ""
                )

                if not title or not link:
                    continue

                if link in processed_links:
                    continue

                all_items.append(
                    {
                        "source": source_name,
                        "title": title,
                        "summary": summary[:1500],
                        "link": link,
                        "published": published,
                    }
                )

        except Exception as exc:
            LOGGER.exception(
                "Failed to read RSS source %s: %s",
                source_name,
                exc,
            )

    # Remove duplicates by link.
    unique_items: list[dict[str, str]] = []
    seen_links: set[str] = set()

    for item in all_items:
        if item["link"] in seen_links:
            continue

        seen_links.add(item["link"])
        unique_items.append(item)

    return unique_items[: int(config["max_news_items"])]


def build_news_context(news_items: list[dict[str, str]]) -> str:
    """Convert news items into structured text for Gemini."""

    sections: list[str] = []

    for index, item in enumerate(news_items, start=1):
        sections.append(
            "\n".join(
                [
                    f"NEWS ITEM {index}",
                    f"Source: {item['source']}",
                    f"Title: {item['title']}",
                    f"Published: {item['published'] or 'Not available'}",
                    f"Summary: {item['summary'] or 'No summary available'}",
                    f"Link: {item['link']}",
                ]
            )
        )

    return "\n\n".join(sections)


def generate_post(
    config: dict[str, Any],
    prompt: str,
    news_items: list[dict[str, str]],
) -> str:
    """Generate one X post using the Gemini API."""

    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    client = genai.Client(api_key=api_key)

    news_context = build_news_context(news_items)

    complete_prompt = f"""
{prompt}

SUPPLIED NEWS:

{news_context}

Accuracy requirements:
- Use only the supplied news.
- Do not claim real-time market data unless it appears above.
- Do not create unsupported numbers.
- Do not include source URLs in the X post.
""".strip()

    LOGGER.info("Sending request to Gemini model: %s", config["model"])

    response = client.models.generate_content(
        model=config["model"],
        contents=complete_prompt,
    )

    generated_text = (response.text or "").strip()

    if not generated_text:
        raise RuntimeError("Gemini returned an empty response.")

    return generated_text


def send_email(
    config: dict[str, Any],
    job_type: str,
    post: str,
    news_items: list[dict[str, str]],
) -> None:
    """Send the generated post through Gmail SMTP."""

    gmail_user = os.getenv("GMAIL_USER")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")

    if not gmail_user:
        raise RuntimeError("GMAIL_USER secret is missing.")

    if not gmail_app_password:
        raise RuntimeError("GMAIL_APP_PASSWORD secret is missing.")

    subject_names = {
        "premarket": "Pre-Market Outlook",
        "breaking": "Important Market Update",
        "midday": "Midday Market Update",
        "sector": "Sector Spotlight",
        "closing": "Market Closing Summary",
    }

    current_time = datetime.now().strftime("%d %b %Y, %I:%M %p")

    max_links = int(config.get("source_links_in_email", 5))
    source_lines = []

    for item in news_items[:max_links]:
        source_lines.append(
            f"- {item['source']}: {item['title']}\n  {item['link']}"
        )

    source_text = "\n".join(source_lines)

    body = f"""
READY TO POST ON X

--------------------------------------------------

{post}

--------------------------------------------------

SOURCE ARTICLES

{source_text}

--------------------------------------------------

Generated at: {current_time}

Review the content before publishing.
""".strip()

    message = EmailMessage()
    message["Subject"] = (
        f"{subject_names[job_type]} - "
        f"{datetime.now().strftime('%d %b %Y')}"
    )
    message["From"] = gmail_user
    message["To"] = config["to_email"]
    message.set_content(body)

    LOGGER.info("Sending email to %s", config["to_email"])

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_user, gmail_app_password)
        smtp.send_message(message)

    LOGGER.info("Email sent successfully.")


def update_history(
    history: dict[str, Any],
    job_type: str,
    news_items: list[dict[str, str]],
) -> None:
    """Update processed news and email history."""

    processed_links = history.setdefault("processed_links", [])
    emails_sent = history.setdefault("emails_sent", [])

    for item in news_items:
        if item["link"] not in processed_links:
            processed_links.append(item["link"])

    emails_sent.append(
        {
            "job_type": job_type,
            "generated_at": datetime.now().isoformat(),
            "news_links": [item["link"] for item in news_items],
        }
    )

    # Prevent unlimited file growth.
    history["processed_links"] = processed_links[-500:]
    history["emails_sent"] = emails_sent[-100:]


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description="Generate and email an Indian market X post."
    )

    parser.add_argument(
        "job_type",
        choices=sorted(VALID_JOB_TYPES),
        help="Type of post to generate.",
    )

    return parser.parse_args()


def main() -> int:
    """Run the complete market content workflow."""

    args = parse_arguments()

    try:
        LOGGER.info("Starting market agent job: %s", args.job_type)

        config = load_json(CONFIG_FILE)
        validate_configuration(config)

        history = load_json(HISTORY_FILE)
        processed_links = set(history.get("processed_links", []))

        news_items = fetch_news(config, processed_links)

        if not news_items:
            LOGGER.warning("No new RSS news items were found.")
            return 0

        LOGGER.info("Collected %d news items.", len(news_items))

        prompt = load_prompt(args.job_type, config)

        post = generate_post(
            config=config,
            prompt=prompt,
            news_items=news_items,
        )

        LOGGER.info("Generated post:\n%s", post)

        send_email(
            config=config,
            job_type=args.job_type,
            post=post,
            news_items=news_items,
        )

        update_history(
            history=history,
            job_type=args.job_type,
            news_items=news_items,
        )

        save_json(HISTORY_FILE, history)

        LOGGER.info("Market agent completed successfully.")
        return 0

    except Exception as exc:
        LOGGER.exception("Market agent failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())