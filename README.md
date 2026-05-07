# DSA Flashcards

A local Streamlit flashcard app for solved LeetCode-style problems.

The app does not hardcode Neetcode 150. Add each problem manually as you solve it, including problems from outside the list. Each saved problem becomes a flashcard with its prompt, pattern notes, and Python solution.

## Features

- Add solved problems manually.
- Import latest submissions from a NeetCode GitHub Sync repository.
- Switch local users by email to test multi-user behavior.
- Upload a question prompt file (`.txt` or `.md`) and a Python solution file (`.py` or `.txt`).
- Track source, difficulty, topics, solved date, and status.
- Review due cards using a simple spaced-repetition algorithm.
- Browse, search, edit, archive, and restore cards.
- Export a JSON backup of all cards and review history.
- Store all data locally in `data/flashcards.db`.
- Use Postgres in production by setting `DATABASE_URL`.

## Review Algorithm

Cards start due immediately. During review, try to recall the approach before revealing the saved solution.

- `Again`: you blanked or missed the main pattern. Due again tomorrow.
- `Hard`: partial recall. The interval grows slowly and ease decreases.
- `Good`: clean recall. The interval grows using the current ease factor.
- `Easy`: automatic recall. The interval grows faster and ease increases.

A card is considered mature once its interval reaches at least 21 days.

## Run

```powershell
pip install -r requirements.txt
python -m streamlit run app.py
```

The app will create the SQLite database automatically on first run.

## GitHub Import

Use the `GitHub Import` page to pull submissions from a repo like:

```text
https://github.com/govanzz/neetcode-submissions
```

The importer reads NeetCode's synced structure, selects the latest `submission-N.py` file per problem by default, and creates or updates flashcards. Manual card entry remains available in the `Add Problem` page.

## Multi-User Direction

The app scopes cards and reviews by `user_email`.

- Locally, use the sidebar `Switch local user` panel to simulate separate users.
- In production, Streamlit OIDC login can provide the signed-in user's email.
- For hosted storage, set `DATABASE_URL` to a Supabase or Neon Postgres connection string.

See [docs/PRODUCTION.md](docs/PRODUCTION.md).
