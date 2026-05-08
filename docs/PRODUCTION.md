# Production Deployment

This app is ready for a hosted production-style setup with:

- Streamlit Community Cloud for the live app.
- Supabase or Neon Postgres for persistent storage.
- Optional Streamlit OIDC login for real user accounts.
- Admin-only monitoring, audit logs, captured error reports, and GitHub import rate limits.

Every card, review, GitHub source, note, and backup is scoped to a `user_email`.

## Current Modes

### Local mode

If no production secrets are configured, the app runs in local mode.

- Use the sidebar `Switch local user` panel to simulate different users.
- Each email gets a separate flashcard library.
- Data is still stored in local SQLite at `data/flashcards.db`.

### Production database mode

When `DATABASE_URL` is configured, the app uses Postgres instead of local SQLite.

Recommended free-start options:

- Supabase Postgres
- Neon Postgres

The app automatically creates the required tables on startup.

### Production login mode

When Streamlit OIDC auth is configured in secrets, the app uses:

- `st.login()` for sign in.
- `st.user.email` as the account identity.
- `st.logout()` for sign out.

Do not commit `.streamlit/secrets.toml`.

## Required Streamlit Secrets

Add these in Streamlit Community Cloud under app settings / secrets:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:PORT/DATABASE"

# Optional GitHub import rate limits per signed-in user.
GITHUB_IMPORTS_PER_HOUR = 5
GITHUB_IMPORTS_PER_DAY = 25

# Optional Google/OIDC login.
# Without this section, the app runs with local email switching.
[auth]
redirect_uri = "https://YOUR-APP.streamlit.app/oauth2callback"
cookie_secret = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
client_id = "GOOGLE_CLIENT_ID"
client_secret = "GOOGLE_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

# Optional admin console access.
[admin]
emails = ["YOUR_EMAIL@gmail.com"]
```

For local production testing, put the same content in `.streamlit/secrets.toml`.

## Deploy Steps

1. Create a Supabase or Neon Postgres database.
2. Copy the Postgres connection string.
3. Push this app to a GitHub repo.
4. Go to Streamlit Community Cloud.
5. Create an app from the GitHub repo.
6. Set the entrypoint file to `app.py`.
7. Paste `DATABASE_URL` into Streamlit secrets.
8. Deploy.

## Notes

- Streamlit Community Cloud deploys from a GitHub repo and lets you add secrets in app settings.
- Supabase provides Postgres connection strings from the project dashboard.
- Streamlit login uses OIDC settings in `[auth]` and exposes the signed-in user through `st.user`.
- The Admin page is only shown when the signed-in email is listed in `[admin].emails`.
- Unexpected app exceptions are recorded in `app_errors`; GitHub imports are tracked in `import_runs`; important actions are tracked in `audit_logs`.

Sources:

- Streamlit deployment: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy
- Streamlit secrets: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management
- Streamlit login: https://docs.streamlit.io/develop/api-reference/user/st.login
- Supabase connection strings: https://supabase.com/docs/reference/postgres/connection-strings

## Target User Flow

1. User signs in with Google/OIDC.
2. App reads their email.
3. User adds their GitHub NeetCode Sync repo.
4. Imported cards are saved under that email.
5. Review queue only shows that user's cards.
6. Notes, exact prompts, and review history stay private to that email.
