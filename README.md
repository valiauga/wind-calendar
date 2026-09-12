# Wind Calendar

Public, subscribe-only kitesurfing wind windows for Dutch spots. Forked from the
forecast/qualification/calendar-push logic in
[wind-window-frame](https://github.com/valiauga/wind-window-frame), repackaged as a
standalone public feature: no personal display, no OAuth for subscribers, no database.

Anyone can add the coast or inland calendar to their own Google Calendar (or subscribe
via ICS in Apple Calendar / Outlook) and see qualified wind sessions pushed there
automatically.

## Architecture

Four Render resources, one repo:

- **Cron Job** (`calendar_sync.py`, hourly): fetches forecasts, applies the reactive
  KNMI correction, qualifies wind windows, and reconciles events into the coast or
  inland public Google Calendar (grouped by each spot's `group` in `spots.json`).
  Render Cron Jobs bill per minute with a $1/month floor — expect to land at that
  floor for an hourly ~1-2 minute run.
- **Static Site** (`public/`): the spot picker. Static sites don't spin down and are
  free with a 100GB/month bandwidth cap at the workspace level — no backend call is
  needed to render the subscribe links, since calendar IDs are fixed at deploy time
  in `public/calendars.json`.
- **Feed proxy** (`feed_server.py`, free Web Service): merges the already-public
  per-spot ICS feeds into one combined feed for whatever subset of spots a visitor
  checks within a region, since Google's native "Add to Google Calendar" button only
  works for a calendar that already exists with fixed content — a visitor-defined
  mix has to be a URL subscription instead. Free tier spins down after 15 min idle,
  which is fine here: this only serves background calendar-app polling, not an
  interactive click a visitor waits on. Also mints and updates persistent "mix"
  tokens (see below).
- **Key Value store** (`wind-calendar-kv`, free tier): backs the persistent tokens.
  Declared in `render.yaml` and wired to the feed proxy's `REDIS_URL` automatically
  via `fromService` — no manual secret copying.

No database beyond that KV store: the reactive correction re-pulls a short trailing
window from KNMI's real-time observations endpoint on every run rather than storing
anything, and Render Cron Jobs can't mount a persistent disk anyway.

### Persistent mix links

A visitor's custom-mix link used to bake their exact spot selection into the URL
itself (`?spots=a,b`), so changing which spots they wanted meant removing the old
calendar subscription and adding a brand new one — no calendar client supports
editing an existing subscription's source URL.

Instead, the site now mints an opaque token the first time a visitor picks a partial
selection (`POST /mix`), remembers it in that browser's `localStorage`, and updates
the same token in place on later changes (`PUT /mix/<token>`) rather than minting a
new one. The subscribe link is `?token=<token>` and never changes, so the visitor's
calendar app just picks up new content on its next scheduled refresh. `kv_store.py`
is a small hand-rolled RESP client (SET/GET with an expiry) so this stays free of
third-party dependencies, same as everything else here; without `REDIS_URL` set,
`/mix` responds `503` and the site falls back to the old static `?spots=` link,
which still works, just needs a resubscribe on change. Tokens are per-browser, not
per-account — a different device or a cleared browser needs its own link.

## Spots

Ten calendars: one per region plus one per individual spot, grouped by
`spots.json`'s `group` field. Every qualifying event is published to both its spot
calendar and its region calendar (`calendar_sync.calendar_keys()`), so a subscriber
can add a whole region in one click or pick just the spots they care about.

- **Coast**: IJmuiden, Wijk aan Zee (proxy via IJmuiden), Slufter/Maasvlakte (via
  Lichteiland Goeree), Zandmotor (Ter Heijde/Kijkduin)
- **Inland**: Muiderberg, Schellinkhout, Medemblik, Edam

This went from one-calendar-per-spot (too much friction for a casual visitor: six
flat "Add" buttons) to two region calendars (simpler, but no way to pick just one
spot) to this: both, with the subscribe page presenting spots nested under their
region so either granularity is one click away.

**Edam**'s actual launch point (per every kite-spot site describing it) sits at
IJsselmeerdijk 24, which is technically in Warder, not Edam — "Edam" is just the
name every spot guide uses for it. Best with easterly wind (`shoreNormal: 90`).

**Zandmotor** is the only spot with a KNMI station left unverified
(`knmiStationId: null`, ships forecast-only) — Hoek van Holland is the nearest
official station but its exact ID hasn't been confirmed against KNMI's catalogue
the way the other stations below were. It's also the only spot gated by tide (see
below): its sheltered lagoon only fills properly around high tide.

Excluded for now, revisit if the situation changes: **Zandvoort** (no verified
coordinates/thresholds yet), **Trintelhaven** (banned for kiting most of the year per
the NKV spot map), **Andijk** (no dedicated or proxy weather station found).

## Tide gate (Zandmotor only)

Zandmotor's shallow lagoon "fills up" around high tide and empties out at low tide,
so `tide.py` filters its qualifying wind windows to ones overlapping a high-tide
window (`tideWindowHours` either side of the peak, in `spots.json`) — a block that
misses every high tide within the fetched horizon is dropped, one that overlaps one
is kept.

It fetches Rijkswaterstaat's **astronomical** (weather-independent) tide prediction
for `tideStation` (currently `hoekvanholland`, the nearest official tide station) via
Waterinfo's chart-widget endpoint — the documented Waterwebservices API
(`OphalenWaarnemingen`) only ever returns past measurements, confirmed by a live
request that silently truncated a future period down to "now"; RWS's forward
predictions are only exposed through this undocumented chart backend, and it only
accepts a handful of whitelisted forward ranges (also found empirically, not
documented) — this uses the widest one confirmed to work, a week out. A block
further out than that, or any fetch failure, ships **ungated** (shown, not hidden):
tide is a refinement here, never a hard dependency, same philosophy as the KNMI
correction below.

Peak detection requires the local maximum to sit above NAP (0cm): Hoek van Holland's
curve is shallow-water-distorted and has a genuine secondary wiggle partway through
the low-water trough (a real local maximum, just nowhere near high tide) that a
shape-only peak check mistakes for one without that floor.

## Reactive KNMI correction

`knmi_correction.py` nudges the near-term forecast toward the current observed/forecast
wind-speed ratio at a spot's mapped KNMI station, decaying back to the raw forecast
over a few hours (tau ~= 5h) so the correction doesn't linger into forecast hours it
has no evidence about. Ratio is clamped to 0.75-1.35.

Only spots with a **confirmed** KNMI station get this: IJmuiden/Wijk aan Zee
(`0-20000-0-06225`) and Slufter/Maasvlakte (Lichteiland Goeree, `0-20000-0-06320`),
both verified against KNMI's EDR station catalogue. Muiderberg, Schellinkhout,
Medemblik and Edam have no KNMI station (their dedicated stations, where they exist,
are NKV-operated with no confirmed public API); Zandmotor's likely station (Hoek van
Holland) hasn't been verified against the catalogue yet either. All four ship
forecast-only, which is a silent no-op in `knmi_correction.apply`, not a guess at an
unverified station.

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
   Authorize your Google account in the browser. This creates the coast and inland
   calendars, attempts to make each public via the Calendar API's ACL (`scope:
   default`), and stores the refresh token and calendar IDs in
   `~/.config/wind-calendar/google-calendar.json` with owner-only permissions.
   If the public-ACL write is rejected (403), share that calendar manually instead:
   **Settings and sharing > Access permissions > Make available to public.**
   Rerunning resumes saved setup; `--reauthorize` renews authorization and keeps IDs.
   Rerun it any time `spots.json` grows a new spot (or region) — it only creates
   and shares whatever calendars are missing, leaving existing ones untouched.
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
