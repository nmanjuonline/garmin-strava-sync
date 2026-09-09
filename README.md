# Garmin → Strava Sync

Pulls your recent Garmin activities (name + gear) and pushes them to the matching
Strava activity, so Strava's activity title and gear stay in sync with Garmin.

**Architecture**
- `backend/` — Flask API. Logs into Garmin Connect (unofficial API, via the
  `garminconnect` library) and calls the Strava API. Must run somewhere with a
  real Python runtime — Render or Fly.io both work (this can't run as a Cloudflare
  Python Worker, since those don't support the libraries this depends on).
- `frontend/` — a single static `index.html`. Lists activities with checkboxes,
  lets you sync one or several at once. Deploy anywhere static: GitHub Pages,
  Cloudflare Pages, or just open the file locally.

---

## 1. Strava setup

1. Go to https://www.strava.com/settings/api and create an API application.
   Note the **Client ID** and **Client Secret**.
2. Get a one-time authorization code by visiting this URL in your browser
   (replace `CLIENT_ID`):

   ```
   https://www.strava.com/oauth/authorize?client_id=CLIENT_ID&redirect_uri=http://localhost&response_type=code&scope=activity:read_all,activity:write
   ```

   Approve it — you'll be redirected to `localhost` with `?code=...` in the URL.
   Copy that `code` value.

3. Exchange it for a refresh token (run once, from any machine with `curl`):

   ```bash
   curl -X POST https://www.strava.com/oauth/token \
     -d client_id=CLIENT_ID \
     -d client_secret=CLIENT_SECRET \
     -d code=THE_CODE_FROM_STEP_2 \
     -d grant_type=authorization_code
   ```

   The response includes a `refresh_token` — save it, this is `STRAVA_REFRESH_TOKEN`
   below. It doesn't expire under normal use (the backend refreshes the short-lived
   access token automatically on every request using this).

## 2. Garmin setup

No app registration needed — just your normal Garmin Connect email + password.
If your account has MFA enabled, the `garminconnect` library will prompt for a
code on first login; for a server deployment, either temporarily disable MFA on
that account or log in once locally to generate a cached session token (see the
`garminconnect` / `garth` docs if you want token-based login instead of
password-based).

## 3. Gear mapping (optional but recommended)

Strava requires an existing `gear_id` (from gear already registered in your
Strava account) — it won't create new gear. To find your Strava gear IDs, once
the backend is running:

```
GET https://your-backend-url/api/strava/gear
```

Then set `GEAR_MAP_JSON` to map Garmin's gear display name to that Strava gear
ID, e.g.:

```json
{"Nike Pegasus 40": "g12345678", "Trek Domane": "b12345678"}
```

If a Garmin activity's gear isn't in this map, the sync still updates the name —
it just skips the gear field.

## 4. Deploy the backend

### Option A — Render

1. Push the `backend/` folder to a GitHub repo (or the whole project — Render
   lets you set the root directory).
2. On Render: New → Web Service → connect the repo.
3. Root directory: `backend`. Build command: `pip install -r requirements.txt`.
   Start command: `gunicorn app:app` (already in the `Procfile`, Render should
   pick it up automatically).
4. Add environment variables from `.env.example` (Environment tab).
5. Deploy. Note the public URL, e.g. `https://your-app.onrender.com`.

### Option B — Fly.io

```bash
cd backend
fly launch    # creates/adjusts fly.toml, don't let it overwrite your config
fly secrets set GARMIN_EMAIL=... GARMIN_PASSWORD=... \
  STRAVA_CLIENT_ID=... STRAVA_CLIENT_SECRET=... STRAVA_REFRESH_TOKEN=... \
  GEAR_MAP_JSON='{"Nike Pegasus 40":"g12345678"}'
fly deploy
```

Note the public URL, e.g. `https://your-app.fly.dev`.

## 5. Deploy the frontend

`frontend/index.html` is fully static — no build step. Options:

- **GitHub Pages**: push `frontend/index.html` to a repo, enable Pages on that
  branch/folder.
- **Cloudflare Pages**: connect the repo, or drag-and-drop the `frontend/`
  folder in the Cloudflare Pages dashboard.
- **Local**: just open `frontend/index.html` in a browser — it works fine
  without any hosting, as long as your backend's CORS allows it (it does, `*`
  by default in `app.py`).

Once it's open, paste your backend URL (from step 4) into the "Backend URL"
field at the top — it's remembered in the browser after that.

## 6. Using it

1. Click **Load activities** — pulls your recent Garmin activities with
   whatever gear Garmin has recorded for each.
2. Click **Sync** on a single row, or check several boxes and click
   **Sync selected**, to push name + gear to the matching Strava activity.
3. Matching is done by activity start time (within `MATCH_TOLERANCE_MINUTES`,
   default 10) — this assumes the activity already exists on Strava (e.g. via
   Garmin's own auto-upload to Strava) and you're just correcting its name/gear
   after the fact.

## Notes / limitations

- Garmin's API is unofficial and undocumented — Garmin can change it any time,
  which may break gear lookups or login. If `/api/activities` starts returning
  empty gear lists, check the `garminconnect` GitHub repo for breaking changes.
- The backend keeps one Garmin session and one Strava token in memory; a free
  Render/Fly instance that spins down on idle will just re-login on the next
  request (a few extra seconds), no action needed.
- Strava rate limits: 200 requests/15 min, 2,000/day on the default API tier —
  fine for personal use, just don't loop over hundreds of activities at once.
