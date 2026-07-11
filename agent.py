from __future__ import annotations

import argparse
import html
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import feedparser
from bs4 import BeautifulSoup
from google import genai


# ---------------------------------------------------------------------------
# File locations
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
HISTORY_FILE = BASE_DIR / "history.json"
PROMPTS_DIR = BASE_DIR / "prompts"


# ---------------------------------------------------------------------------
# Supported job types
# ---------------------------------------------------------------------------

VALID_JOB_TYPES = {
    "premarket",
    "breaking",
    "midday",
    "sector",
    "closing",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

LOGGER = logging.getLogger("market-agent")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Load a JSON file.

    If the file does not exist and a default is provided, return the default.
    """

    if not path.exists():
        if default is not None:
            return default

        raise FileNotFoundError(f"Required file does not exist: {path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            loaded_data = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(loaded_data, dict):
        raise ValueError(f"Expected a JSON object in {path}")

    return loaded_data


def save_json(path: Path, data: dict[str, Any]) -> None:
    """Save a dictionary as formatted JSON."""

    with path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )

        file.write("\n")


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_html(value: str) -> str:
    """Convert HTML from an RSS entry into clean plain text."""

    decoded_value = html.unescape(value or "")
    soup = BeautifulSoup(decoded_value, "html.parser")
    plain_text = soup.get_text(" ", strip=True)

    return " ".join(plain_text.split())


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def validate_configuration(config: dict[str, Any]) -> None:
    """Validate required configuration settings."""

    required_keys = [
        "to_email",
        "word_count",
        "hashtags_count",
        "language",
        "tone",
        "max_news_items",
        "rss_sources",
    ]

    missing_keys = [
        key
        for key in required_keys
        if key not in config
    ]

    if missing_keys:
        raise ValueError(
            "Missing required config values: "
            + ", ".join(missing_keys)
        )

    if not isinstance(config["to_email"], str):
        raise ValueError("to_email must be a string.")

    if not config["to_email"].strip():
        raise ValueError("to_email cannot be empty.")

    if not isinstance(config["rss_sources"], list):
        raise ValueError("rss_sources must be a list.")

    if not config["rss_sources"]:
        raise ValueError("At least one RSS source is required.")

    if int(config["word_count"]) <= 0:
        raise ValueError("word_count must be greater than zero.")

    if int(config["hashtags_count"]) < 0:
        raise ValueError("hashtags_count cannot be negative.")

    if int(config["max_news_items"]) <= 0:
        raise ValueError("max_news_items must be greater than zero.")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def load_prompt(job_type: str, config: dict[str, Any]) -> str:
    """Load and format the prompt for the selected job."""

    prompt_file = PROMPTS_DIR / f"{job_type}.txt"

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Prompt file does not exist: {prompt_file}"
        )

    template = prompt_file.read_text(encoding="utf-8")

    try:
        return template.format(
            word_count=config["word_count"],
            hashtags_count=config["hashtags_count"],
            language=config["language"],
            tone=config["tone"],
        )
    except KeyError as exc:
        raise ValueError(
            f"Unknown placeholder in prompt file {prompt_file}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# RSS collection
# ---------------------------------------------------------------------------

def fetch_news(
    config: dict[str, Any],
    processed_links: set[str],
) -> list[dict[str, str]]:
    """Collect unused news entries from the configured RSS sources."""

    collected_items: list[dict[str, str]] = []

    for source in config["rss_sources"]:
        if not isinstance(source, dict):
            LOGGER.warning("Skipping invalid RSS source configuration.")
            continue

        source_name = str(
            source.get("name", "Unknown source")
        ).strip()

        source_url = str(
            source.get("url", "")
        ).strip()

        if not source_url:
            LOGGER.warning(
                "Skipping RSS source without a URL: %s",
                source_name,
            )
            continue

        LOGGER.info("Reading RSS source: %s", source_name)

        try:
            feed = feedparser.parse(
                source_url,
                request_headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(compatible; XMarketEmailAgent/1.0)"
                    )
                },
            )

            if getattr(feed, "bozo", False):
                LOGGER.warning(
                    "RSS parsing warning for %s: %s",
                    source_name,
                    getattr(feed, "bozo_exception", "Unknown warning"),
                )

            for entry in getattr(feed, "entries", []):
                title = clean_html(
                    str(entry.get("title", ""))
                )

                summary = clean_html(
                    str(
                        entry.get("summary")
                        or entry.get("description")
                        or ""
                    )
                )

                link = str(
                    entry.get("link", "")
                ).strip()

                published = str(
                    entry.get("published")
                    or entry.get("updated")
                    or ""
                ).strip()

                if not title or not link:
                    continue

                if link in processed_links:
                    continue

                collected_items.append(
                    {
                        "source": source_name,
                        "title": title,
                        "summary": summary[:2000],
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

    # Remove duplicate links while preserving order.
    unique_items: list[dict[str, str]] = []
    seen_links: set[str] = set()

    for item in collected_items:
        link = item["link"]

        if link in seen_links:
            continue

        seen_links.add(link)
        unique_items.append(item)

    maximum_items = int(config["max_news_items"])

    return unique_items[:maximum_items]


# ---------------------------------------------------------------------------
# Gemini context
# ---------------------------------------------------------------------------

def build_news_context(
    news_items: list[dict[str, str]],
) -> str:
    """Convert the collected news into structured prompt context."""

    sections: list[str] = []

    for index, item in enumerate(news_items, start=1):
        sections.append(
            "\n".join(
                [
                    f"NEWS ITEM {index}",
                    f"Source: {item['source']}",
                    f"Title: {item['title']}",
                    (
                        "Published: "
                        f"{item['published'] or 'Not provided'}"
                    ),
                    (
                        "Summary: "
                        f"{item['summary'] or 'No summary provided'}"
                    ),
                    f"Link: {item['link']}",
                ]
            )
        )

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Gemini model selection
# ---------------------------------------------------------------------------

def normalize_model_name(model_name: str) -> str:
    """Remove the optional 'models/' prefix from a model name."""

    clean_name = model_name.strip()

    if clean_name.startswith("models/"):
        clean_name = clean_name.removeprefix("models/")

    return clean_name


def get_model_name(model: Any) -> str:
    """Safely read the name from a model object."""

    name = getattr(model, "name", "")

    return normalize_model_name(str(name))


def get_supported_actions(model: Any) -> list[str]:
    """Read supported model actions from different SDK representations."""

    possible_attributes = [
        "supported_actions",
        "supported_generation_methods",
    ]

    for attribute_name in possible_attributes:
        value = getattr(model, attribute_name, None)

        if value:
            return [
                str(action).lower()
                for action in value
            ]

    return []


def model_supports_generation(model: Any) -> bool:
    """Check whether a listed model supports content generation."""

    actions = get_supported_actions(model)

    # Some SDK responses may not expose supported actions.
    # In that situation, retain Gemini text models as candidates.
    if not actions:
        model_name = get_model_name(model).lower()

        return (
            "gemini" in model_name
            and "embedding" not in model_name
            and "image" not in model_name
            and "live" not in model_name
            and "tts" not in model_name
        )

    return any(
        action in {
            "generatecontent",
            "generate_content",
        }
        for action in actions
    )


def select_available_models(
    client: genai.Client,
    config: dict[str, Any],
) -> list[str]:
    """
    Get usable models for the current API key and order them by preference.

    The configured model is tried first only when the API reports that it is
    available. Remaining compatible Flash text models are used as fallbacks.
    """

    LOGGER.info("Checking models available to this Gemini API key.")

    available_models: list[Any] = []

    try:
        available_models = list(client.models.list())
    except Exception as exc:
        raise RuntimeError(
            f"Unable to list Gemini models: {exc}"
        ) from exc

    generation_models: list[str] = []

    for model in available_models:
        model_name = get_model_name(model)

        if not model_name:
            continue

        if not model_supports_generation(model):
            continue

        generation_models.append(model_name)

    # Remove duplicate names while preserving order.
    generation_models = list(dict.fromkeys(generation_models))

    if not generation_models:
        raise RuntimeError(
            "The Gemini API key returned no text-generation models."
        )

    configured_models: list[str] = []

    # Supports either:
    # "model": "gemini-x"
    # or
    # "models": ["gemini-x", "gemini-y"]
    configured_model = config.get("model")

    if isinstance(configured_model, str) and configured_model.strip():
        configured_models.append(
            normalize_model_name(configured_model)
        )

    configured_model_list = config.get("models", [])

    if isinstance(configured_model_list, list):
        for model_name in configured_model_list:
            if isinstance(model_name, str) and model_name.strip():
                configured_models.append(
                    normalize_model_name(model_name)
                )

    # Current preferred lightweight and Flash model families.
    preferred_names = [
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3-flash",
        "gemini-flash-latest",
    ]

    ordered_models: list[str] = []

    def add_when_available(candidate: str) -> None:
        normalized_candidate = normalize_model_name(candidate)

        if (
            normalized_candidate in generation_models
            and normalized_candidate not in ordered_models
        ):
            ordered_models.append(normalized_candidate)

    for model_name in configured_models:
        add_when_available(model_name)

    for model_name in preferred_names:
        add_when_available(model_name)

    # Add other Flash text models reported by the API.
    for model_name in generation_models:
        lowered_name = model_name.lower()

        if (
            "flash" in lowered_name
            and "image" not in lowered_name
            and "live" not in lowered_name
            and "tts" not in lowered_name
        ):
            add_when_available(model_name)

    # Finally add any remaining usable text-generation model.
    for model_name in generation_models:
        add_when_available(model_name)

    LOGGER.info(
        "Available generation model candidates: %s",
        ", ".join(ordered_models),
    )

    return ordered_models


# ---------------------------------------------------------------------------
# Gemini response extraction
# ---------------------------------------------------------------------------

def extract_response_text(response: Any) -> str:
    """Extract generated text safely from a Gemini SDK response."""

    # Preferred SDK convenience property.
    try:
        response_text = getattr(response, "text", None)

        if isinstance(response_text, str) and response_text.strip():
            return response_text.strip()
    except Exception:
        pass

    # Safe fallback: candidates -> content -> parts -> text.
    candidates = getattr(response, "candidates", None) or []

    extracted_parts: list[str] = []

    for candidate in candidates:
        content = getattr(candidate, "content", None)

        if content is None:
            continue

        parts = getattr(content, "parts", None) or []

        for part in parts:
            part_text = getattr(part, "text", None)

            if isinstance(part_text, str) and part_text.strip():
                extracted_parts.append(part_text.strip())

    generated_text = "\n".join(extracted_parts).strip()

    if generated_text:
        return generated_text

    # Include finish reason in the error when available.
    finish_reasons: list[str] = []

    for candidate in candidates:
        finish_reason = getattr(candidate, "finish_reason", None)

        if finish_reason is not None:
            finish_reasons.append(str(finish_reason))

    reason_text = (
        ", ".join(finish_reasons)
        if finish_reasons
        else "not provided"
    )

    raise RuntimeError(
        "Gemini returned no text. "
        f"Finish reason: {reason_text}"
    )


# ---------------------------------------------------------------------------
# Gemini generation
# ---------------------------------------------------------------------------

def generate_post(
    config: dict[str, Any],
    prompt: str,
    news_items: list[dict[str, str]],
) -> tuple[str, str]:
    """
    Generate one X post.

    Returns:
        A tuple containing:
        - generated post
        - model used
    """

    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY GitHub secret is missing."
        )

    client = genai.Client(api_key=api_key)

    model_candidates = select_available_models(
        client=client,
        config=config,
    )

    news_context = build_news_context(news_items)

    complete_prompt = f"""
{prompt}

SUPPLIED NEWS:

{news_context}

MANDATORY ACCURACY RULES:

- Use only the supplied news.
- Do not invent index values.
- Do not invent stock prices.
- Do not invent percentage changes.
- Do not claim data is live unless the supplied news explicitly says so.
- Do not include article URLs inside the X post.
- Do not produce markdown headings.
- Return only one finished post suitable for X.
""".strip()

    generation_errors: list[str] = []

    for model_name in model_candidates:
        LOGGER.info(
            "Trying Gemini model: %s",
            model_name,
        )

        try:
            response = client.models.generate_content(
                model=model_name,
                contents=complete_prompt,
            )

            generated_text = extract_response_text(response)

            LOGGER.info(
                "Post generated successfully using model: %s",
                model_name,
            )

            return generated_text, model_name

        except Exception as exc:
            error_message = str(exc)

            LOGGER.warning(
                "Model %s failed: %s",
                model_name,
                error_message,
            )

            generation_errors.append(
                f"{model_name}: {error_message}"
            )

    error_summary = "\n".join(generation_errors)

    raise RuntimeError(
        "All available Gemini models failed.\n"
        f"{error_summary}"
    )


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def send_email(
    config: dict[str, Any],
    job_type: str,
    post: str,
    news_items: list[dict[str, str]],
    model_used: str,
) -> None:
    """Send the generated X post through Gmail SMTP."""

    gmail_user = os.getenv("GMAIL_USER", "").strip()

    gmail_app_password = (
        os.getenv("GMAIL_APP_PASSWORD", "")
        .replace(" ", "")
        .strip()
    )

    if not gmail_user:
        raise RuntimeError(
            "GMAIL_USER GitHub secret is missing."
        )

    if not gmail_app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD GitHub secret is missing."
        )

    recipient = str(config["to_email"]).strip()

    subject_names = {
        "premarket": "Pre-Market Outlook",
        "breaking": "Important Market Update",
        "midday": "Midday Market Update",
        "sector": "Sector Spotlight",
        "closing": "Market Closing Summary",
    }

    generated_time = datetime.now(
        timezone.utc
    ).astimezone()

    maximum_source_links = int(
        config.get("source_links_in_email", 5)
    )

    source_lines: list[str] = []

    for item in news_items[:maximum_source_links]:
        source_lines.append(
            "\n".join(
                [
                    f"- {item['source']}: {item['title']}",
                    f"  {item['link']}",
                ]
            )
        )

    source_text = "\n\n".join(source_lines)

    email_body = f"""
READY TO POST ON X

--------------------------------------------------

{post}

--------------------------------------------------

SOURCE ARTICLES

{source_text}

--------------------------------------------------

Generated at: {generated_time.strftime('%d %b %Y, %I:%M %p %Z')}
Gemini model: {model_used}

Review the generated post before publishing.
""".strip()

    message = EmailMessage()

    message["Subject"] = (
        f"{subject_names[job_type]} - "
        f"{generated_time.strftime('%d %b %Y')}"
    )

    message["From"] = gmail_user
    message["To"] = recipient

    message.set_content(email_body)

    LOGGER.info(
        "Sending email to %s",
        mask_email(recipient),
    )

    try:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=30,
        ) as smtp:
            smtp.login(
                gmail_user,
                gmail_app_password,
            )

            smtp.send_message(message)

    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            "Gmail authentication failed. Verify GMAIL_USER and "
            "GMAIL_APP_PASSWORD. Use the Google App Password, not "
            "your normal Gmail password."
        ) from exc

    LOGGER.info("Email sent successfully.")


def mask_email(email_address: str) -> str:
    """Mask an email address before printing it in logs."""

    if "@" not in email_address:
        return "***"

    local_part, domain = email_address.split("@", maxsplit=1)

    if len(local_part) <= 2:
        masked_local = "***"
    else:
        masked_local = (
            local_part[:2]
            + ("*" * max(3, len(local_part) - 2))
        )

    return f"{masked_local}@{domain}"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def update_history(
    history: dict[str, Any],
    job_type: str,
    news_items: list[dict[str, str]],
    model_used: str,
) -> None:
    """Record processed links and successful email history."""

    processed_links = history.setdefault(
        "processed_links",
        [],
    )

    emails_sent = history.setdefault(
        "emails_sent",
        [],
    )

    if not isinstance(processed_links, list):
        processed_links = []

    if not isinstance(emails_sent, list):
        emails_sent = []

    for item in news_items:
        link = item["link"]

        if link not in processed_links:
            processed_links.append(link)

    emails_sent.append(
        {
            "job_type": job_type,
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "model": model_used,
            "news_links": [
                item["link"]
                for item in news_items
            ],
        }
    )

    # Keep history files reasonably small.
    history["processed_links"] = processed_links[-500:]
    history["emails_sent"] = emails_sent[-100:]


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    """Read the requested job type from the command line."""

    parser = argparse.ArgumentParser(
        description=(
            "Generate and email an Indian market X post."
        )
    )

    parser.add_argument(
        "job_type",
        choices=sorted(VALID_JOB_TYPES),
        help="Type of market post to generate.",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the complete market email workflow."""

    arguments = parse_arguments()
    job_type = arguments.job_type

    try:
        LOGGER.info(
            "Starting market agent job: %s",
            job_type,
        )

        config = load_json(CONFIG_FILE)
        validate_configuration(config)

        history = load_json(
            HISTORY_FILE,
            default={
                "processed_links": [],
                "emails_sent": [],
            },
        )

        processed_links = set(
            history.get("processed_links", [])
        )

        news_items = fetch_news(
            config=config,
            processed_links=processed_links,
        )

        if not news_items:
            LOGGER.warning(
                "No unused RSS news items were found. "
                "No email will be sent."
            )

            return 0

        LOGGER.info(
            "Collected %d news items.",
            len(news_items),
        )

        prompt = load_prompt(
            job_type=job_type,
            config=config,
        )

        post, model_used = generate_post(
            config=config,
            prompt=prompt,
            news_items=news_items,
        )

        LOGGER.info(
            "Generated post:\n%s",
            post,
        )

        send_email(
            config=config,
            job_type=job_type,
            post=post,
            news_items=news_items,
            model_used=model_used,
        )

        update_history(
            history=history,
            job_type=job_type,
            news_items=news_items,
            model_used=model_used,
        )

        save_json(
            HISTORY_FILE,
            history,
        )

        LOGGER.info(
            "Market agent completed successfully."
        )

        return 0

    except Exception as exc:
        LOGGER.exception(
            "Market agent failed: %s",
            exc,
        )

        return 1


if __name__ == "__main__":
    sys.exit(main())