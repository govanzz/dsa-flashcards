# DSA Flashcards

A Streamlit flashcard app for turning solved DSA problems into a personal spaced-repetition review queue.

The app is built for solved LeetCode and NeetCode-style problems. You can add cards manually, import the latest files from a NeetCode GitHub Sync repository, write memory notes, practice from recall, and rate each review so the next due date adapts to how well you remembered the pattern.

## Features

- Dashboard with active cards, due cards, new cards, mature cards, seven-day review count, retention progress, and upcoming reviews.
- Manual card intake with title, URL, source, difficulty, status, solved date, topics, exact prompt, pattern notes, and saved solution.
- Optional prompt and solution upload for `.txt`, `.md`, and Python source files while adding a card.
- GitHub importer for NeetCode GitHub Sync repos that picks the latest `submission-N` file per problem.
- Import support for Python, JavaScript, TypeScript, Java, C++, C#, Go, Rust, Kotlin, Swift, and SQL submissions.
- Personal review queue scoped by `user_email`, with local user switching for development and optional Streamlit OIDC login in production.
- Practice workspace for writing an approach or code attempt before revealing the saved solution.
- Review ratings: `Again`, `Hard`, `Good`, and `Easy`.
- Browse/edit view with search, filters, archived-card handling, card edits, and review history.
- JSON backup export for the active user's cards and review events.
- SQLite for local use, or Postgres in production through `DATABASE_URL`.
- Admin console for import usage, audit logs, captured app errors, and GitHub import guardrails.

## App Pages

- `Dashboard`: review metrics, retention progress, upcoming cards, and memory strategy.
- `Add Problem`: create a card manually and optionally seed the prompt or solution from uploaded files.
- `GitHub Import`: save a repo source and import latest synced submissions into the active user's queue.
- `Review`: recall the problem, write an attempt, reveal the saved solution, then rate recall.
- `Browse / Edit`: search cards, update metadata and notes, archive/restore cards, and inspect review history.
- `Backup`: download a JSON backup of cards and reviews.
- `Admin`: available only to configured admins; shows operational monitoring data.

## Local Setup

```powershell
pip install -r requirements.txt
python -m streamlit run app.py
```

The app creates `data/flashcards.db` automatically on first run. If no auth secrets are configured, the sidebar shows a `Switch local user` panel so you can test separate libraries by email.

## Configuration

Configuration can come from environment variables or Streamlit secrets.

| Setting | Required | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | No | Use Postgres instead of the local SQLite database. |
| `GITHUB_IMPORTS_PER_HOUR` | No | Per-user hourly GitHub import limit. Defaults to `5`. |
| `GITHUB_IMPORTS_PER_DAY` | No | Per-user 24-hour GitHub import limit. Defaults to `25`. |
| `ADMIN_EMAILS` | No | Comma/space-separated admin emails when using environment variables. |
| `[admin].emails` | No | Admin email list when using Streamlit secrets. |
| `[auth]` | No | Streamlit OIDC login configuration. When present, the app uses `st.login()` and `st.user.email`. |

Use [docs/secrets.example.toml](docs/secrets.example.toml) as a starting point for Streamlit secrets. Do not commit `.streamlit/secrets.toml`.

## GitHub Import

The importer accepts a GitHub URL or `owner/repo`, plus branch, language, max-card limit, and an optional GitHub token for private repos or higher API limits.

It scans the repository tree for files shaped like:

```text
topic/problem-slug/submission-1.py
topic/problem-slug/submission-2.py
```

For each problem directory, the app imports only the highest-numbered `submission-N` file for the selected language. Imported cards keep their review schedule when refreshed; only the saved solution/code is updated when `Refresh solutions for existing cards` is enabled.

## Review Scheduling

New cards are due immediately. After each review:

- `Again`: resets repetitions, lowers ease, records a lapse, and schedules the card for tomorrow.
- `Hard`: grows the interval slowly and lowers ease.
- `Good`: moves from 1 day to 3 days, then multiplies by the current ease factor.
- `Easy`: starts at 4 days, then 7 days, then grows faster and increases ease.

A card is considered mature once its interval reaches at least 21 days.

## Production

For hosted use, configure:

- Streamlit Community Cloud for the app.
- Supabase or Neon Postgres through `DATABASE_URL`.
- Optional Streamlit OIDC login for real user accounts.
- Optional admin emails for the Admin page.

The app initializes its database tables on startup for both SQLite and Postgres. See [docs/PRODUCTION.md](docs/PRODUCTION.md) for deployment notes.
