# X Market Email Agent

This project collects Indian stock market and economy news from RSS feeds, uses Gemini to generate one ready-to-post X post, and sends it to Gmail.

It does not automatically post to X.

## Daily schedule

- 8:30 AM IST: Pre-market outlook
- 10:30 AM IST: Important market update
- 12:30 PM IST: Midday market update
- 2:30 PM IST: Sector spotlight
- 4:15 PM IST: Closing summary

Runs Monday to Friday using GitHub Actions.

## Required GitHub secrets

- `GEMINI_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

## Manual test

Open:

Actions → Indian Market Email Agent → Run workflow

Select the required job type and run it.

## Configuration

Update `config.json` to change:

- Email recipient
- Word count
- Language
- Tone
- Gemini model
- RSS sources
- Number of news items

## Important

Always review generated content before posting it publicly.

The system must not be treated as a source of investment advice.