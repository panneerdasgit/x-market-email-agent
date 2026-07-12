# X Market Content Agent — Version 2.2

This agent creates short Indian stock-market posts, emails them to Gmail, and leaves final publishing to you.

## News sources

1. Moneycontrol market stories through Google News RSS
2. LiveMint official Markets RSS

The agent balances selected news across both sources where possible. Source names and URLs are not included in the email post.

## Weekly schedule

### Monday-Friday

| IST time | Content |
|---|---|
| 8:30 AM | Pre-market outlook |
| 10:30 AM | Important market update |
| 12:30 PM | Stock in focus |
| 2:30 PM | Beginner education |
| 4:15 PM | Closing mood and next-session watch |

### Saturday

| IST time | Content |
|---|---|
| 10:00 AM | Weekly recap |
| 6:00 PM | Investing lesson |

### Sunday

| IST time | Content |
|---|---|
| 10:00 AM | Next-week outlook |
| 6:00 PM | Market question |

## Market holidays

The 2026 NSE equity-market holidays are stored in `config.json`.

On a configured weekday holiday:

- 8:30 AM pre-market becomes `holiday_notice`
- 10:30 AM breaking is skipped
- 12:30 PM stock focus becomes `holiday_company_spotlight`
- 2:30 PM weekday education is skipped
- 4:15 PM closing becomes `holiday_next_trading_day`

That creates three useful holiday posts instead of five normal trading-session posts.

Update `market_holidays` annually from the official NSE holiday circular.

## Prompt names

The filenames clearly identify weekday, Saturday, Sunday and holiday use:

- `01_weekday_premarket.txt`
- `02_weekday_breaking.txt`
- `03_weekday_stock_focus.txt`
- `04_weekday_education.txt`
- `05_weekday_closing.txt`
- `06_saturday_weekly_recap.txt`
- `07_saturday_investing_lesson.txt`
- `08_sunday_next_week_outlook.txt`
- `09_sunday_market_question.txt`
- `10_holiday_notice.txt`
- `11_holiday_company_spotlight.txt`
- `12_holiday_next_trading_day.txt`

## GitHub secrets

Create:

- `GEMINI_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

Use the Google App Password, not your normal Gmail password.

## Manual testing

Open:

`Actions → X Market Content Agent V2.2 → Run workflow`

Manual runs use the selected prompt directly. They do not redirect according to today's holiday, so every weekday, weekend and holiday prompt can be tested at any time.

## History

The workflow commits `history.json` after successful email delivery, allowing Gemini to avoid recent wording.

The Friday closing run keeps only the latest configured history records.

Before making local changes:

```bash
git pull origin main
```

## Notes

- Review every email before posting.
- Default limit: 250 characters.
- The agent never posts directly to X.
- Version 2.2 is text-only; it does not create charts or images.
