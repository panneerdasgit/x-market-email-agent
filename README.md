# X Market Email Agent — Version 2.0

This agent:

1. Reads Indian market news from Moneycontrol RSS.
2. Uses a Gemini model available to your API key.
3. Generates one short X post of at most 250 characters.
4. Emails the post to Gmail.
5. Does **not** post automatically to X.

## Daily schedule

| IST time | Content |
|---|---|
| 8:30 AM | Pre-market outlook |
| 10:30 AM | Important market update |
| 12:30 PM | Stock in focus |
| 2:30 PM | Beginner market education |
| 4:15 PM | Closing summary |

Runs Monday to Friday.

## Required GitHub secrets

Create these under:

`Repository → Settings → Secrets and variables → Actions`

- `GEMINI_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

Use the 16-character Google App Password, not your normal Gmail password.

## Manual test

Open:

`Actions → X Market Email Agent V2 → Run workflow`

Choose a content type and run it.

## Files to customise

### `config.json`

Change:

- recipient email
- character limit
- hashtags
- tone
- education topics
- RSS source

### `prompts/`

Each content type has its own prompt.

## Notes

- Review every generated post before publishing.
- The system avoids investment recommendations and guaranteed-return language.
- Version 2 does not generate chart images. Chart generation should be added later only after connecting a reliable numerical market-data source.
