# Wind Calendar

Public, subscribe-only kitesurfing wind windows for Dutch spots. Forked from the
forecast/qualification/calendar-push logic in
[wind-window-frame](https://github.com/valiauga/wind-window-frame), repackaged as a
standalone public feature: no personal display, no OAuth for subscribers, no database.

Anyone can add a spot's calendar to their own Google Calendar (or subscribe via ICS in
Apple Calendar / Outlook) and see qualified wind sessions pushed there automatically.

## Architecture

Two Render resources, one repo:

- **Cron Job** (`calendar_sync.py`, hourly): fetches forecasts, applies the reactive
  KNMI correction, qualifies wind windows, and reconciles events into each spot's
  public Google Calendar. Render Cron Jobs bill per minute with a $1/month floor —
  expect to land at that floor for an hourly ~1-2 minute run.
- **Static Site** (`public/`): the spot picker. Static sites don't spin down and are
  free with a 100GB/month bandwidth cap at the workspace level — no backend call is
  needed to render the subscribe links, since calendar IDs are fixed at deploy time
  in `public/calendars.json`.

No database: the reactive correction re-pulls a short trailing window from KNMI's
real-time observations endpoint on every run rather than storing anything, and Render
Cron Jobs can't mount a persistent disk anyway.

## v1 spots

IJmuiden, Wijk aan Zee (proxy via IJmuiden), Slufter/Maasvlakte (via Lichteiland
Goeree), Muiderberg, Schellinkhout, Medemblik. Each has its own calendar
(`spots.json`) — one calendar per spot, not regional clusters, since kiting
conditions are spot-specific enough that clustering would put irrelevant sessions on
a subscriber's calendar.

Excluded for now, revisit if the situation changes: **Zandvoort** (no verified
coordinates/thresholds yet), **Trintelhaven** (banned for kiting most of the year per
the NKV spot map), **Warder** and **Andijk** (no dedicated or proxy weather station
found).

## Reactive KNMI correction

`knmi_correction.py` nudges the near-term forecast toward the current observed/forecast
wind-speed ratio at a spot's mapped KNMI station, decaying back to the raw forecast
over a few hours (tau ~= 5h) so the correction doesn't linger into forecast hours it
has no evidence about. Ratio is clamped to 0.75-1.35.

Only spots with a **confirmed** KNMI station get this: IJmuiden/Wijk aan Zee
(`0-20000-0-06225`) and Slufter/Maasvlakte (Lichteiland Goeree, `0-20000-0-06320`),
both verified against KNMI's EDR station catalogue. Muiderberg, Schellinkhout and
Medemblik have no KNMI station (their dedicated stations, where they exist, are
NKV-operated with no confirmed public API) — those three ship forecast-only, which is
a silent no-op in `knmi_correction.apply`, not a guess at an unverified station.

Requires a registered KNMI EDR API key as `KNMI_EDR_API_KEY`. Without it, correction
is skipped entirely and every spot ships forecast-only.

The 30-day rolling per-spot/per-direction-bin bias correction from the source project
is explicitly cut from v1 — not enough samples per bin at this data volume to be
trustworthy. Revisit only if the reactive-only version shows a repeated pattern at the
same spot/direction over months of real use.

## Owner setup (once, on your computer)

Requires Python 3.10+ with Europe/Amsterdam timezone data (included on macOS and
Render). No third-party Python packages are required.

1. In [Google Cloud Console](https://console.cloud.google.com/), create/select a
   project, enable **Google Calendar API**, and configure Google Auth Platform.
   Use an External audience, this app's URL as the homepage, and
   `<app-url>/privacy.html` as the privacy policy URL. Add only
   `https://www.googleapis.com/auth/calendar.app.created` to Data Access.
2. Create an OAuth client of type **Desktop app** and download its client JSON
   outside this repository.
3. Run:
   ```sh
   python3 calendar_setup.py /absolute/path/to/client_secret.json
   ```
   Authorize your Google account in the browser. This creates one calendar per spot,
   attempts to make each public via the Calendar API's ACL (`scope: default`), and
   stores the refresh token and calendar IDs in
   `~/.config/wind-calendar/google-calendar.json` with owner-only permissions.
   If the public-ACL write is rejected (403), share that calendar manually instead:
   **Settings and sharing > Access permissions > Make available to public.**
   Rerunning resumes saved setup; `--reauthorize` renews authorization and keeps IDs.
4. Copy the printed `calendars` mapping into `public/calendars.json` (see
   `public/calendars.example.json` for the shape) and commit it — calendar IDs aren't
   secret once the calendar is public.
5. For unattended use, move the OAuth app out of **Testing** before obtaining the
   long-lived refresh token; Testing refresh tokens for Calendar scopes expire after
   seven days.

### Render deployment

`render.yaml` declares both resources. Set these environment variables on the
**cron job** service (never on the static site):

- `GOOGLE_CALENDAR_CONFIG`: the full JSON from `~/.config/wind-calendar/google-calendar.json`.
- `KNMI_EDR_API_KEY`: optional; omit to ship forecast-only for every spot.
- `WIND_APP_URL`: defaults to `https://wind-calendar.onrender.com/`; override if the
  deployed URL differs.

Preview current forecast events without Google credentials or writes:

```sh
python3 calendar_sync.py --dry-run
```

### Checks

```sh
python3 -m unittest discover -s tests -v
```
