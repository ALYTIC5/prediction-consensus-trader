# Phase 8 design: operations console

No application code yet - this is the design for the FastAPI + Jinja2 +
htmx dashboard, written up before touching `app/dashboard/`, per CLAUDE.md's
working agreement.

## Flags and assumptions

1. **New Python dependencies - CLAUDE.md's Stack section needs updating.**
   FastAPI needs `fastapi`, an ASGI server (`uvicorn`), and `python-multipart`
   (FastAPI requires it to parse the tuning panel's form POSTs) added to
   `pyproject.toml`; `jinja2` too, though FastAPI pulls it in as an optional
   dependency already. CLAUDE.md's working agreement says "ask before adding
   a dependency not listed under Stack" - the brief already settled the
   framework choice explicitly, so this isn't really a question, but the
   Stack section is currently stale against that decision and should be
   updated as part of implementing this phase, not silently left out of date.

2. **`runtime_overrides` as specified can't produce the "override log."**
   The table is `key (unique), value, updated_at` - one row per setting,
   overwritten in place on every change. That's exactly right for "what's
   the current override," but the Tuning page also asks for an "override
   log list (key, old, new, when)" - a history of every change, which a
   single-row-per-key table structurally cannot hold once a key is changed
   twice (the old value is gone the moment the new one is written). Adding
   a second, append-only table for this - `override_log(id, key, old_value,
   new_value, changed_at)`, one row written alongside every write to
   `runtime_overrides` - rather than silently reinterpreting "log" as just
   "current value," which would quietly drop the audit trail this page
   exists to provide.

3. **`get_effective_settings()` must not be cached the way `get_settings()`
   is.** `get_settings()` is `@lru_cache` - one Settings instance for the
   whole process, which is correct for env-derived config that never
   changes without a restart. `get_effective_settings()` has the opposite
   job: read `runtime_overrides` fresh and overlay it on top of the cached
   base, every single call, so a tuning change is visible on the very next
   scorer/generator cycle. Concretely: `get_effective_settings()` calls the
   cached `get_settings()` for the base, then does its own uncached DB read
   over `runtime_overrides` and returns a new overlaid object each time -
   it is never itself decorated with `@lru_cache`.

4. **That requires touching how scorer.py and generator.py get their
   settings, not just adding a new function.** Today, `app/main.py` builds
   one `Settings` object at startup and closes over it in every
   `PeriodicJob`'s lambda (`run=lambda: generate_signals(settings)`) -
   `scripts/collect_once.py` does the same. That captured object is fixed
   for the life of the process. For "tuning takes effect next cycle without
   restart" to actually be true, `run_scoring()` and `generate()` need to
   stop trusting a settings object handed to them from outside and instead
   call `get_effective_settings()` themselves, at the top of their own
   cycle. This is a real code change to existing Phase 2/3 functions, not
   just new Phase 8 code - flagging it here so it's a conscious part of this
   phase's implementation plan rather than a surprise when the override
   panel silently does nothing until a restart. Collectors are unaffected -
   they don't read any of the tunable fields, so they keep taking `settings`
   the way they do today.

5. **The tunable registry is exactly the `consensus_*`/`signal_*` threshold
   fields, minus the interval ones - everything else is out of the Tuning
   UI entirely this phase, not hidden as a security matter, just out of
   scope.** Concretely:
   - **Tunable live**: `consensus_min_traders`, `consensus_min_weighted_score`,
     `consensus_min_combined_value_usd`, `consensus_freshness_hours`,
     `consensus_include_increases`, `signal_min_liquidity_usd`,
     `signal_min_volume_24h_usd`, `signal_price_min`, `signal_price_max`,
     `signal_max_spread`, `signal_min_hours_to_end`, `signal_ttl_hours`.
   - **Listed, `restart_required`**: `leaderboard_interval_seconds`,
     `positions_interval_seconds`, `markets_interval_seconds`,
     `consensus_interval_seconds`.
   - **Never adjustable or displayed**: `postgres_*`, `redis_url`,
     `gamma_api_base`, `data_api_base`.
   - **Not shown on this page at all**: everything else - `scoring_lookback_days`,
     `score_weight_month/all_time/consistency`, `leaderboard_top_n`,
     `tracked_wallets_limit`, `positions_size_threshold`,
     `http_timeout_seconds`, `http_max_concurrency`, etc. Scoring weights in
     particular would be a reasonable candidate for a future tuning
     surface, but the brief scoped this to consensus/signal thresholds only
     and adding more is a bigger decision (mid-flight scoring changes are
     more structural than a liquidity floor) than this phase should make on
     its own.

6. **`consensus_include_increases` is a bool, not a bounded number** - the
   registry's "type, min, max" shape needs a case for it: `type=bool`,
   `min`/`max` both `None`, rendered in the Tuning UI as a toggle instead of
   a numeric input with bounds.

7. **Chart.js should be vendored, not pulled from a CDN.** The brief doesn't
   say which; "no build step" is satisfied either way since Chart.js is a
   single script tag either way, no bundler involved. Vendoring the file
   into `app/dashboard/static/` means the console still renders if outbound
   internet is down or slow - a monitoring tool for trading-adjacent
   infrastructure shouldn't have its own charts depend on a third-party CDN
   being up, and it keeps the console's only network dependency being the
   local Postgres/Redis it already needs.

8. **Heartbeat freshness thresholds aren't specified - proposing concrete
   cutoffs.** "dot + relative time" per collector needs a rule for when a
   dot is phosphor (fresh) vs amber (stale) vs alert (dead). Proposing:
   fresh = last write age ≤ 1× the collector's interval, stale = between 1×
   and 3×, dead = beyond 3× or no write ever recorded. This is a
   dashboard-only display rule, not a Settings field - it doesn't affect
   collector behavior, so it isn't part of the tunable registry, just a
   constant in the dashboard code that can be adjusted later without a
   migration.

## Architecture

**Process and binding.** `app/dashboard/` is its own FastAPI app, started as
its own process (`uvicorn app.dashboard.app:app --host 127.0.0.1 --port
8000`, no code needed beyond the ASGI app object - no new wrapper script),
never imported into or launched from `app.main`'s scheduler process. Keeping
it separate means a dashboard crash or a slow page render can never take
down data collection, and vice versa - the two have nothing in common at
runtime except the database.

**No authentication, and that's fine specifically because of the binding.**
Bound to `127.0.0.1` only, so it's reachable exclusively from the same
machine (or over an SSH tunnel someone deliberately sets up) - never
directly from the network. That's the entire justification; it stops being
true the moment anyone binds this to `0.0.0.0` or puts it behind a reverse
proxy, which is explicitly out of scope until Phase 9 brings real auth. The
stakes here are also lower than they might sound: per CLAUDE.md's hard
read-only rule, nothing this console can do - not even the tuning POSTs -
places, signs, or cancels a real order. Worst case for now is someone
mistunes a liquidity floor.

**Stack: server-rendered, htmx for freshness, Chart.js for the few real
charts.** Jinja2 templates render full pages; htmx handles the
auto-refreshing fragments (heartbeat row, event tape, funnel, logs) by
polling small HTML-returning endpoints and swapping them in, rather than
shipping a JSON API and a client-side rendering framework. No React, no
Node, no npm build step, no new Python web framework beyond FastAPI itself -
this keeps the whole console inside the stack CLAUDE.md already commits to,
and means "run the dashboard" never means anything beyond `uv run uvicorn
...`.

**Strict read/write split.** Every `GET` is a pure read - no endpoint that
renders a page or a fragment is allowed to write anything, ever. The only
mutating routes in the entire app are the tuning panel's `POST` endpoints
(apply an override, reset to default) - everything else, including the
htmx-polled fragments, is `GET`. This makes the console trivially safe to
leave open in a browser tab and poll forever, and makes "did this page just
change something" never a question worth asking.

**Pages**: Overview, Signals, Traders, Markets, Events, Tuning, Logs, plus a
`/healthz` JSON endpoint (for a process supervisor or a future uptime
check - not for humans, no template).

**Reserved Paper PnL panel.** Overview always renders a "Paper PnL" panel;
today its only content is the empty-state copy "Paper trading arrives in
Phase 4." The slot and its layout position are real now so Phase 4 fills it
in with a real component later without reflowing the rest of the page.

## Backend enablers

**`consensus_runs`** - one row per generator cycle, written by
`app/signals/generator.py` at the end of `_run_cycle()` (alongside its
existing funnel-line logging, not instead of it). Columns: `executed_at`,
then plain `Integer` columns for every funnel stage - `events`, `groups`,
`market`, `liquidity`, `price`, `spread`, `breadth`, `quality`,
`conviction`, `new_signals`, `reinforced`. These are counts, not money, so
they're ordinary integers, not the `Money` `NUMERIC(24,6)` type - that
convention is for dollar/price/size/PnL values specifically. Indexed on
`executed_at` for the "funnel-over-time" chart's `ORDER BY executed_at DESC
LIMIT N` query pattern.

**`runtime_overrides`** (`key` unique, `value` string, `updated_at`) plus
the proposed **`override_log`** (`id`, `key`, `old_value`, `new_value`,
`changed_at`) - see flag 2. Both written together by the tuning POST
handler in one transaction: upsert `runtime_overrides`, insert a row into
`override_log`. `value` is stored as a string on both, same reasoning as
every other "plain string, not a native type" column already in this
schema (`position_history.event_type`, `signals.status`) - the registry in
`app/config/adjustable.py` is what knows how to parse and validate it back
into the real type, so the DB layer doesn't need its own type system for
this.

**`app/config/adjustable.py`** - a plain registry, e.g. a list of small
dataclasses (`key`, `settings_field`, `type`, `min`, `max`, `description`,
`restart_required: bool`), one entry per field from flag 5's tunable and
restart-required lists. This is the single source of truth the tuning POST
handler validates against (reject any key not in the registry, reject any
value outside `[min, max]` or of the wrong type) and that the Tuning page
renders itself from - a field only ever needs to be added or removed in one
place.

**`get_effective_settings()`** - lives in `app/config/settings.py` next to
`get_settings()`. Loads the cached base settings, reads every row currently
in `runtime_overrides`, and for each one that matches a registered,
in-bounds key, overlays it onto a copy of the base settings object. A
`runtime_overrides` row that doesn't match anything in the registry
(leftover from a since-removed field, or corrupted some other way) is
skipped and logged as a warning, not raised - this function runs at the top
of every scorer and generator cycle, so a single bad row must never be able
to take the whole pipeline down. See flag 3 (why this isn't cached) and
flag 4 (what else needs to change for this to actually take effect).

**File logging.** `app/utils/logging.py`'s `setup_logging()` currently does
one `logging.basicConfig(..., stream=sys.stdout, force=True)` call. Adding
file output means attaching a second handler explicitly - a
`RotatingFileHandler("logs/polybot.log", maxBytes=5*1024*1024,
backupCount=3)` alongside the existing stdout `StreamHandler` - since
`basicConfig` only ever configures a single handler chain.`logs/` is
git-ignored (operational output, not source, same reasoning as
`graphify-out/`).

## Visual design system - "surveillance terminal"

The brief's framing is exact: Bloomberg terminal crossed with mission
control, not a neon cyberpunk skin. Every visual choice below is in service
of scanning a lot of numbers fast, not atmosphere.

**Tokens.** Defined once as CSS custom properties on `:root`, every color
elsewhere in the stylesheet derived from these seven, never a new hex value
introduced ad hoc in a component:

| Token | Value | Role |
|---|---|---|
| `--ink` | `#0B0E14` | page background |
| `--panel` | `#10141D` | raised panel background |
| `--grid` | `#1C2230` | hairline borders/rules |
| `--text` | `#E6EDF3` | primary text |
| `--dim` | `#8A93A6` | secondary text, labels |
| `--phosphor` | `#5CF2C7` | the one accent - live data, links, active state, OK |
| `--amber` | `#FFB454` | warnings, reinforced, restart-required |
| `--alert` | `#F26D78` | errors, expired, breached thresholds |

One accent family, phosphor - amber and alert are status semantics, never
used decoratively (never a border color chosen "because it looks nice,"
always because something is warning or wrong). No gradients, no purple, no
glass/blur effects - anything that reads as "cyberpunk cosplay" rather than
"instrument panel" is out.

**Type.** IBM Plex Mono for everything that's data - numbers, tables,
labels, code-like content; IBM Plex Sans only for the rare paragraph of
running prose (page descriptions, empty-state copy). Micro-labels (column
headers, section tags) are 11px uppercase, `--dim`, `letter-spacing:
0.08em`. Every numeric column is right-aligned with `font-variant-numeric:
tabular-nums` and a fixed decimal count per column, so a stack of numbers
lines up on the decimal point without any manual padding.

**Texture and motion, restrained.** A near-invisible background grid (2-3%
opacity) on the page background only, never inside panels or over text.
1px `--grid` borders everywhere a rule is needed - no heavier borders, no
drop shadows standing in for elevation. Status dots are small squares, not
circles (squares read as "indicator light on a panel," circles read as
"bullet point"): phosphor with a slow pulse for fresh, amber static for
stale, alert static for dead. htmx fragment swaps may flash the affected
row's left border in phosphor for 300ms so an auto-refreshing table doesn't
just silently change under the reader's eye. `prefers-reduced-motion:
reduce` disables both the pulse and the flash outright - they become static
color with no animation, never removed as information (the dot's color
still says fresh/stale/dead). No scanlines, no glow on body text, no
animated backgrounds - texture stays in the 2-3% grid and nowhere else.

**The signature element: the funnel cascade.** A horizontal row of labeled
bars, one per filter stage in evaluation order (events → groups → market →
liquidity → price → spread → breadth → quality → conviction → new/
reinforced), each bar's width proportional to its survivor count at that
stage. Each stage dims slightly relative to the one before it (a gradient
of brightness, not of hue - still just phosphor, just less of it), and the
count that got rejected at that step bleeds off the bar's trailing edge in
`--alert`, so the eye reads loss at every stage without needing a separate
legend. The cascade ends in a small phosphor-bright stub showing the
new/reinforced split - the only fully-saturated phosphor in the whole
element, since that's the actual output the rest of the chain exists to
produce. This is the one place the design is allowed to be bold; everything
else on the page should look quiet next to it. It appears twice: compact
(latest run only) at the top of Overview, and larger with a history strip
underneath it on the Signals page (backed by `consensus_runs`).

**Copy.** Sentence case, plain verbs on every control - "Apply override,"
"Reset to default," never "Submit" or "Go." Empty states always name the
next useful action instead of just stating absence - e.g. "No active
signals. The funnel below shows where the last run's candidates were
rejected," not just "No signals." Errors state what went wrong and what to
do about it, never an apology - "Value must be between 0.01 and 1.00,"
never "Sorry, something went wrong."

**Quality floor.** Every text/background pairing actually used
(`--text`/`--dim` on `--ink`/`--panel`) checked for real contrast, not just
picked because phosphor-on-near-black looks cool. Keyboard focus is always
visible - a phosphor outline on the focused element, never suppressed.
Layout holds together down to a narrow window (tables scroll horizontally
inside their own container before anything wraps badly). Every drill-down
(a signal's contributor list, a trader's position history, a market's price
sparkline) is reachable and operable from the keyboard, not mouse/hover-only.

## Pages

**Overview.** Heartbeat row: one entry per collector (leaderboard,
positions, markets, consensus), each showing its status dot (flag 8's
fresh/stale/dead rule) and a relative-time "last write" derived from that
collector's own latest timestamp - `MAX(leaderboard_snapshots.captured_at)`,
`MAX(position_history.detected_at)`, `MAX(market_history.captured_at)`,
`MAX(consensus_runs.executed_at)` - no new heartbeat table needed, the data
already exists. Count strip: tracked wallets, open positions, fresh events
in the last 24h, active signals - four cheap aggregate queries. The funnel
cascade for the latest `consensus_runs` row. The reserved Paper PnL panel.
A bottom "tape": the most recent ~15 `position_history` rows (bootstrap
always excluded here, unconditionally - this is a live-activity feed, not
an audit view), one line per event, htmx-polled every 5s, color-coded by
`event_type` (OPENED phosphor, CLOSED dim, INCREASED/DECREASED plain text).

**Signals.** The ACTIVE signals table (age, title, outcome, traders,
weighted score, combined value, entry vs. current price, liquidity,
expires-in - same shape `scripts/show_signals.py` already prints as text,
now as an HTML table with a per-row drill-down). The drill-down fragment
unpacks the row's own `contributors` JSONB - address, username, weight,
event type, size, price, detected time per contributor - plus the stored
market metrics, so it never has to re-query `position_history`; this is the
same "explainable from its own row" principle from CLAUDE.md's Code
conventions, now surfaced in the UI instead of just the DB schema. Below
that: recent EXPIRED and reinforced signal history, and the larger funnel
cascade with its `consensus_runs` history strip.

**Traders.** Score table - rank, username, address in short form
(`0x1234…abcd`), the three score components each as a small horizontal bar
(so relative weight is visible without reading three numbers), final score,
and the tracked status dot. Clicking a row opens the wallet view: that
wallet's open positions and recent `position_history` events - effectively
the same tape component as Overview, scoped to one wallet.

**Markets.** Markets currently held by at least one tracked wallet's open
position - question, outcome prices, liquidity, 24h volume, spread,
ends-in, with an open/closed filter defaulting to open (closed markets are
available but not the default view - they're not actionable). Row
drill-down renders a Chart.js sparkline of that asset's price history from
`prices` - the one place a real chart earns its keep over a table.

**Events.** The full `position_history` feed as its own page: filterable
by event type and by bootstrap (default excluding bootstrap, but - unlike
Overview's tape - togglable here, since this page's job is investigation,
not just "what's happening now"), newest first, auto-refreshing, capped at
200 rows server-side so the query and the page both stay cheap regardless
of how much history accumulates.

**Tuning.** One row per entry in the `app/config/adjustable.py` registry:
name, its one-line plain-language description, default value, current
override (or a dash if none is set), the effective value actually in use,
its bounds, and either an inline apply form (tunable fields) or an amber
"restart required" tag (the four scheduler intervals - listed for
visibility, since hiding them would make the page look like it doesn't know
they exist, but genuinely not editable from here). A reset-to-default
control per tunable row deletes its `runtime_overrides` row (falling back
to the base `Settings` value) and still writes an `override_log` entry, so
resets show up in the audit trail same as changes. The override log itself
renders as a simple list below the table (key, old value, new value, when),
newest first.

**Logs.** Tails the last 200 lines of `logs/polybot.log`, newest at the
bottom (matches how a terminal naturally reads, unlike Events' newest-first
table), auto-refreshing every 5s, each line colored by its log level
(WARNING amber, ERROR alert, everything else plain text), with a pause
toggle so a reader mid-investigation isn't fighting the feed while it keeps
moving underneath them.
