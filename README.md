# X Market Content Agent — Version 2.3 Stable

Version 2.3 focuses on natural, beginner-friendly content.

## Main improvements

- Simple English
- Short rotating headers
- No forced greetings
- No Sunday questions
- Sunday investing tips instead
- Banned AI-sounding finance phrases
- One clear idea per post
- Preferred length: 170–230 characters
- Hard limit: 250 characters
- Friday history cleanup
- Moneycontrol and LiveMint sources
- Weekday, Saturday, Sunday and holiday prompts

## Schedule

### Monday-Friday
- 8:30 AM — Pre-market
- 10:30 AM — Important update
- 12:30 PM — Stock focus
- 2:30 PM — Education
- 4:15 PM — Closing

### Saturday
- 10:00 AM — Weekly recap
- 6:00 PM — Investing lesson

### Sunday
- 10:00 AM — Week ahead
- 6:00 PM — Investing tip

## Required secrets

- `GEMINI_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

## Important configuration

`config.json` contains:

- character limit
- banned phrases
- rotating headers
- content voice
- education topics
- Sunday tip topics
- news sources
- market holidays

`market_holidays` is intentionally empty. Add the current official NSE holiday dates before relying on automatic holiday redirection.

## Manual test

Open:

`Actions → X Market Content Agent V2.3 Stable → Run workflow`

Choose a content type and run it.

Manual runs bypass holiday redirection so every prompt can be tested at any time.

## Git note

The workflow commits `history.json`.

Before editing locally:

```bash
git pull origin main
```

## Posting

The agent only emails content. Review it, then copy and paste it into X.
