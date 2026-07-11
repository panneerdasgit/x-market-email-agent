# X Market Email Agent — Version 2.1

Version 2.1 reads the latest Moneycontrol RSS items on every scheduled news run.
It allows the same day's news to be reused from different angles while using recent
post history to discourage repeated wording.

It emails one ready-to-post X post and never posts automatically to X.

## Daily schedule

| IST time | Content |
|---|---|
| 8:30 AM | Pre-market outlook |
| 10:30 AM | Important market update |
| 12:30 PM | Stock in focus |
| 2:30 PM | Beginner market education |
| 4:15 PM | Closing summary |

Runs Monday to Friday.

## Version 2.1 changes

- Removed processed-link blocking.
- Every news job reads the latest RSS items.
- Recent generated posts are sent to Gemini to reduce repetition.
- History stores structured post records.
- Friday closing run keeps only the latest 25 post and email records.
- Email contains no news URLs.
- Every post is restricted to the configured character limit.

## Required GitHub secrets

- `GEMINI_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

## Manual test

Open `Actions → X Market Email Agent V2.1 → Run workflow`, choose a content type, and run it.

## Important Git note

The workflow commits `history.json` after successful runs. Before making local changes, run:

```bash
git pull origin main
```

Then edit, commit and push.

## Chart support

Version 2.1 is text-only. Add chart generation later only after connecting a reliable structured market-data source.
