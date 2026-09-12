# Sierra Chart -> TopstepX Manual Mirror

> **Read this first.** This tool places real orders on a real TopstepX
> account, one-way, with no reconcile. An order can be missed, doubled, or
> left resting after Sierra Chart is flat. Test it on a practice account, read
> [Limitations](#limitations) and the [risk disclosure](LICENSE) before you
> put money behind it, and confirm that mirroring trades is permitted under
> [TopstepX's rules](#check-topsteps-rules-first) and those of any prop firm
> whose account you connect. You are responsible for every order it sends.

Shadow-copies the orders you place **by hand** in Sierra Chart onto a TopstepX
account. Trade your primary account from Sierra Chart as usual; a second
account on TopstepX follows.

- **Watch it run:** [How to Trade TopstepX From Sierra Chart](https://youtu.be/PGZLzlDdw9I).
  The full setup, then a live bracket and OCO test on a practice account.
- **Written guide:** [Sierra Chart to TopstepX trade copier](https://onepersontradedesk.com/blog/sierra-chart-topstepx-trade-copier).
  The setup step by step, what it costs, and how to test it on a practice account.
- **More free plumbing like this:** [get the newsletter](https://onepersontradedesk.com/resources?utm_source=github&utm_medium=repo&utm_campaign=topstepx-mirror-repo&utm_content=readme).
  One short brief a week from building a one-person trade desk, and the
  optd-starter repo when you sign up.

```
Sierra Chart manual order  --scan-->  Manual_Mirror.cpp (ACSIL study)
                                            |
                                     JSONL outbox file
                                            |
                                     manual_bridge.py (Python)
                                            |
                                     TopstepX REST API
```

Two parts:

- **`sierra/Manual_Mirror.cpp`** - a Sierra Chart study. Polls the order list
  of the trade account selected on its chart, diffs it against an in-memory
  snapshot, and appends one JSON line per order state transition to a daily
  outbox file. Never places an order in Sierra Chart itself.
- **`bridge/manual_bridge.py`** - a Python service. Tails the outbox and turns
  each line into a `/api/Order/place` or `/api/Order/cancel` call.

Supported order types: **market, limit, stop**. Anything else (stop-limit,
trailing stop, MIT, ...) is logged and skipped.

One-way. Sierra Chart is the brain, TopstepX is the shadow. Nothing on
TopstepX ever feeds back into Sierra Chart. Read [Limitations](#limitations)
before you put money behind it.

It is a mirror, not a trading strategy. It never decides when to enter or
exit: it waits for an order you placed and translates it into the format the
ProjectX API expects. With no order from you, it has nothing to copy. It
won't give you an edge either. It moves an order from the platform you
already trade on to the account you want it on, and it can't tell you whether
that trade is any good.

---

## Requirements

- Sierra Chart on an **Integrated** service package, with trading enabled on
  the account you want to mirror FROM. TopstepX does not feed market data into
  Sierra Chart, so if Topstep is your only account you still need Sierra
  Chart's own data feed for a chart and DOM to trade from. See
  [What it costs](#what-it-costs).
- A TopstepX account. Start on the free **practice account**: ProjectX has no
  sandbox, and Topstep's own advice is to test against a practice account. It
  uses the same endpoints as an evaluation or funded account.
- ProjectX API access and an API key. In the ProjectX dashboard, subscribe to
  API access (the code **`topstep`** takes 50% off), link your TopstepX
  profile, and generate the key. The key can place trades, so treat it like a
  password: never commit it and never show it on screen. It lives only in
  `bridge/manual_bridge.env`, which is gitignored.
- Python 3.9+. The two packages it needs (`pyyaml`, `requests`) install
  themselves on first run if you use `start-bridge.bat`; otherwise
  `python -m pip install -r requirements.txt`.
- Windows. The study writes the outbox with the Win32 API and Sierra Chart is
  a Windows application; the bridge itself is plain Python.

---

## What it costs

The code is free. Running it is not. Prices were checked on 2026-09-05. They
change, so check the linked pages.

| Line | Monthly | Note |
|---|---|---|
| [Sierra Chart Integrated Standard](https://www.sierrachart.com/index.php?page=doc/Packages.php) (Service Package 10) | $36 | The cheapest package that works. The Base package can't use the Denali data feed. |
| [Denali data feed](https://www.sierrachart.com/index.php?page=doc/DenaliExchangeDataFeed.php), CME only, with market depth | $13.50 | Covers ES, NQ, MES and MNQ. For other exchanges, see the Denali page. |
| [ProjectX API access](https://help.topstep.com/en/articles/11187768-topstepx-api-access) | $29, or **$14.50 with the code `topstep`** | A public 50% code, listed as permanent on Topstep's page. |
| **Total** | **about $64** | Before any Topstep evaluation fee. |

That's the minimum the mirror needs. If you use order-flow tools (footprint,
Numbers Bars, a full DOM ladder), you may want Integrated Advanced (Service
Package 11), which costs more. Pick the package for how you trade, not for
this tool.

---

## Check Topstep's rules first

Read the rules for the account stage you are actually trading, on Topstep's
own help pages. As checked on 2026-09-05:

- **Trading Combine** and **Express Funded Account:** automated strategies are
  allowed, with conditions.
  ([Combine](https://help.topstep.com/en/articles/8284197-trading-combine-parameters) ·
  [Express Funded](https://help.topstep.com/en/articles/8284215-express-funded-account-parameters))
- **Live Funded Account:** the help page says automated strategies are
  permitted, but automated trading through the ProjectX API is prohibited.
  ([Live Funded](https://help.topstep.com/en/articles/10657969-live-funded-account-parameters))
- **Personal device only.** All trading activity has to come from your own
  device: no VPS, no VPN, no remote servers.
  ([API access](https://help.topstep.com/en/articles/11187768-topstepx-api-access))
  The mirror runs on the Windows machine your Sierra Chart runs on. Don't
  host it anywhere else.

These are Topstep's rules, not an interpretation of them, and they change.
Re-read them before you connect. Every stage's rule, quoted with its source:
[How to Trade a Topstep (TopstepX) Account from Sierra Chart](https://onepersontradedesk.com/blog/automate-topstep-sierra-chart).

---

## Setup (one time)

### 1. Clone into your Sierra Chart folder

Clone this repo **inside your Sierra Chart install folder**, named
`sc-topstepx-mirror`. Every default that ships here is already filled in for
that layout, so if you follow it there is no path for you to invent.

```powershell
cd C:\SierraChart
git clone https://github.com/drewautomates/optd-sierrachart-topstepx-mirror.git sc-topstepx-mirror
cd sc-topstepx-mirror\bridge
copy manual_bridge.example.env manual_bridge.env
copy manual_config.example.yaml manual_config.yaml
```

You should end up with:

```
C:\SierraChart\                      your Sierra Chart install
├── ACS_Source\                      the study is built from here (step 4)
├── Data\
└── sc-topstepx-mirror\              this repo
    ├── outbox\                      already exists - the study writes here
    ├── bridge\
    └── sierra\
```

The `outbox\` folder ships with the clone, so there is nothing to create.

> **Installed somewhere else?** That is fine, and you do not have to edit the
> config for it. The bridge always looks for the `outbox\` folder sitting next
> to `bridge\` in its own clone, wherever that is. The only place you type a
> path is the study's *Outbox Directory* input in step 5 - and
> `--doctor` prints the exact string to paste.

Both config copies are gitignored. Edit `manual_bridge.env`:

```
TSX_USER=your_topstepx_username
TSX_API_KEY=your_api_key
```

### 2. Find your TopstepX account id

```powershell
python manual_bridge.py --config manual_config.yaml --env manual_bridge.env --list-accounts
```

Paste the integer `id` of the account you want to mirror **into** as
`accountId` in `manual_config.yaml`. This is a real account: orders placed
there are real orders.

### 3. Contract map and paths

In `manual_config.yaml`:

- `contracts:` - short key -> TopstepX contract id. The keys are what the
  study's *Symbol->Contract Map* input produces (`MNQ`, `NQ`, `ES`, `MES` by
  default). Note the full-size E-mini roots on ProjectX are `EP` and `ENQ`,
  not `ES`/`NQ`; the micros keep their tickers. **Edit at every contract roll.**
  The ids in the example file are a sample month. **Verify them before you
  trade;** they are not a promise they are current:
  - The month in `contracts:` must match the month TopstepX is trading
    **and** the month on your Sierra Chart chart (the *Scan Symbols* input
    in step 5). If the three disagree, orders go to the wrong contract or
    never mirror.
  - Look ids up with ProjectX contract search (`POST /api/Contract/search`,
    see the [ProjectX API docs](https://gateway.docs.projectx.com/)). It only
    returns the month ProjectX marks as active, so in roll week it can still
    show the old month. Check the new one with `POST /api/Contract/searchById`.
  - `--doctor` warns when a mapped month has **expired**. It can't tell you
    that a still-valid month is the wrong one, such as last quarter's
    contract during roll week.
- `paths:` - **leave the whole block commented out.** The bridge defaults to
  the `outbox\` folder shipped in this clone, which is right wherever you put
  it. Set `paths.outbox:` only if you want the study writing somewhere else.

Now check your work before touching Sierra Chart:

```powershell
python manual_bridge.py --doctor
```

It verifies credentials, config, folders, contract months and your TopstepX
account, prints `PASS`/`FAIL` for each, and ends with the exact path to paste
into the study in step 5. Everything must say `PASS` before you go on.

### 4. Deploy and build the study

Copy `sierra/Manual_Mirror.cpp` and `sierra/tsx_manual_emit.h` into your
Sierra Chart `ACS_Source` folder (it sits inside the Sierra Chart install
folder, next to `Data/`). The scripts do that for you and refuse to guess the
path:

```powershell
cd C:\SierraChart\sc-topstepx-mirror
powershell -ExecutionPolicy Bypass -File sierra\deploy.ps1 -Target "C:\SierraChart\ACS_Source"
```

```bash
bash sierra/deploy.sh "/path/to/SierraChart/ACS_Source"
```

Then in Sierra Chart: **Analysis -> Build Custom Studies DLL -> Build ->
`Manual_Mirror`**. Watch the build output for the success line.

This is the most technical step of the setup, and it is configuration, not
coding: move two files, press Build. If the build fails, paste the full build
output into Claude Code or ChatGPT and ask what to fix.

### 5. Add the study to ONE chart

Open the chart you trade from, select the trade account you want to mirror
FROM, then **Analysis -> Studies -> Add Custom Study -> Manual_Mirror ->
TopstepX Manual Mirror**. Inputs:

| Input | Set to |
|---|---|
| **Enable Manual Mirror** | **No** for now. This is the master switch. |
| **Symbol->Contract Map** | `MNQ=MNQ;NQ=NQ;ES=ES;MES=MES` (default). `substr=key` pairs; longest substring wins, so `MES` beats `ES`. The key must exist in `contracts:` in the YAML. |
| **Scan Throttle (ms)** | `50` (default). See the cadence note below. |
| **Verbose Logging** | **Yes** for the first few sessions. |
| **Freshness Window (ms)** | `1000` (default). See [Freshness guard](#freshness-guard). |
| **Scope To This Chartbook** | **No** unless you run automated trading studies in the same Sierra Chart instance. See [Safety notes](#safety-notes). |
| **Scan Symbols** | The EXACT symbols to watch, `;` separated, as shown in the chart header, e.g. `ESZ6.CME;MESZ6.CME;NQZ6.CME;MNQZ6.CME`. A symbol not listed **never mirrors**. **Edit at every contract roll.** |
| **Outbox Directory** | The path `--doctor` printed. See below. |
| **Close-If-Open On Fill (stop/limit)** | **No** until the live test in [Close-If-Open](#close-if-open) passes, then Yes. An input flip, no rebuild. |

**Outbox Directory** is the one path you have to type, and it is the one
thing that reliably goes wrong. Do not guess it - run

```powershell
python manual_bridge.py --doctor
```

and paste the path it prints under *"must be exactly"*. If you cloned into
`C:\SierraChart` as in step 1, that will be:

```
C:\SierraChart\sc-topstepx-mirror\outbox
```

Type it with **single** backslashes; this is a Sierra Chart input box, not
YAML. If the input is left **blank**, nothing is ever emitted and the message
log says so.

Run exactly **one** instance of this study per Sierra Chart process. It
mirrors every symbol in its Scan Symbols list; a second instance is refused
with a message-log line.

### 6. Scan cadence

The study runs on every chart update, then throttles itself. Its effective
cadence is the **larger** of *Scan Throttle* and Sierra Chart's **Chart Update
Interval** (Global Settings -> General Settings; default 500 ms). If you want
the 50 ms default to mean anything, lower the chart update interval. The
bootstrap banner prints both numbers so you can see what you actually got.

---

## Every trading day

**1. Start the bridge.** Double-click **`start-bridge.bat`** in the repo
root. It runs the setup check first and refuses to start if anything fails,
so a bad config can never look like a working one.

Or from the `bridge/` folder - `--config` and `--env` default to the files
next to the script, so there is nothing to pass:

```powershell
python manual_bridge.py
```

You should see:

```
INFO tsx.client: auth: logging in as <user>
INFO tsx.client: auth: token acquired
INFO tsx.manual: recover: N orders on account today, M tags in local seen_tags
INFO tsx.manual: manual-bridge: started (dry_run=False) account=<id>
INFO tsx.manual: manual-bridge: contract map = {'MNQ': ...}
INFO tsx.manual: manual-bridge: outbox=... poll_interval=0.10s
```

If login fails, fix env/config before going further. `--dry-run` runs the
whole pipeline with no API calls; use it to sanity-check before the open.

**2. Confirm you are flat on both sides.** Sierra Chart position = 0, TopstepX
portal position = 0, no working orders. Bootstrap assumes a clean slate.

**3. Flip the study's Enable to Yes.** The message log prints
`Manual_Mirror: bootstrap done (0 existing orders marked seen, ...)`. From
this moment every new order on a scanned symbol mirrors.

**4. Place a tiny test order.** A 1-contract limit far from the market, so it
rests. Within a scan interval plus a poll you should see the bridge log
`place: label=manual-<id>-v1 ...` and a working order in the TopstepX portal.
Cancel it in Sierra Chart; the bridge logs `cancel: order_id=...` and the
portal order disappears. Only now are you live.

**Keep the TopstepX portal open all session.** The mirror runs one way, so
anything you do inside TopstepX is invisible to Sierra Chart. Place every
order from Sierra Chart. When the two sides disagree, TopstepX is the record
of truth. They can drift even if you follow that rule: a partial fill, a limit
that fills on one side only, a crashed bridge or a failed API call that never
reaches Topstep. The portal's **Flatten All** is your emergency stop.

**End of day:** flatten and cancel everything in Sierra Chart as usual (the
bridge mirrors the cancels), flip Enable to No, Ctrl-C the bridge, and confirm
the TopstepX portal is flat with no stray working orders.

---

## How the diff mirror works

On every scan tick the study loops `sc.GetOrderForSymbolAndAccountByIndex()`
over each symbol in Scan Symbols on the chart's selected trade account,
resolves each order's symbol to a contract key through the Symbol->Contract
map, and compares each order against its last-seen snapshot:

| Transition | Emitted |
|---|---|
| New, working, market/limit/stop, **fresh** | `place_{type}` (tag v1) |
| New, already **filled**, fresh | catch-up `place_market` (a fast fill the scan missed) |
| New, but **stale** (older than the window) | recorded as seen, **no emit** |
| New, working, unsupported type | log + skip |
| New, canceled/error | recorded as seen, no emit |
| Known, working -> canceled | `cancel` |
| Known, working -> filled, market type | **no-op** (the TopstepX twin fills on its own trigger) |
| Known, working -> filled, stop/limit, Close-If-Open = No | **no-op** |
| Known, working -> filled, stop/limit, Close-If-Open = Yes | `close_if_open` (see below) |
| Known, working, price or qty changed | `cancel_replace` (version bumps only when an emit goes out) |
| Known, working, then vanished from the list | `cancel` if Sierra Chart confirms CANCELED; `close_if_open` if it confirms FILLED (stop/limit, Close-If-Open = Yes) |

**Bootstrap rule.** On the first scan after Enable, every order already
visible is recorded as seen and not mirrored. Only orders placed after the
study is running mirror. Re-enable only when flat.

### Freshness guard

A first-seen order is mirrored only if its `LastActivityTime` is within the
Freshness Window (default 1000 ms). An order first seen when already older
than that is treated as *history that just appeared in the list* (a
trade-account switch, a study reload, a global Sim-mode toggle) and is
recorded as seen but not mirrored. This is admission control at birth only:
once mirrored, an order is tracked for its whole life, so long-resting limit
and stop orders are unaffected.

Why: without it, switching the trade account while armed makes the new
account's entire order and fill history look like new orders, and the
catch-up path would fire a market order on TopstepX for every historical
fill. The window is floored at `throttle + 750 ms` so it can never be tighter
than the scan cadence.

### Close-If-Open

**The gap.** A resting stop or limit that is repriced INTO the market (drag
the stop through price to exit) fills within milliseconds. No scan cadence can
catch the working window, so the study only ever sees `working -> FILLED`, a
designed no-op, and the TopstepX twin keeps its **old** price. The shadow
position rides unmanaged until that price trades. Same class: a Sierra Chart
limit that fills while the TopstepX twin does not (queue position).

**The fix.** With *Close-If-Open On Fill* = Yes, every mirrored stop/limit
that goes `working -> FILLED` emits `close_if_open`. The bridge then:

1. No twin tracked for this `sc_id`: no-op.
2. Asks TopstepX for its open orders. Twin **not** open: TopstepX already
   filled (or cancelled) it, in sync, no-op (`twin_gone`).
3. Twin **is** open: cancel it. Cancel fails: it filled in the gap, no-op.
   Cancel **succeeds**: the twin was still resting, so TopstepX did NOT fill.
   Fire a **market** for the same side and size (`caught_up`, logged at
   WARNING).

The market is gated on the cancel succeeding, so it can never double-fill.
The open-orders query is a second, independent check against a cancel API
that might report success on an already-filled order. Market orders are
excluded because their twin never rests. Known limitation: a TopstepX
*partial* fill on the twin is not detectable, so the catch-up is for the
full size.

**Live test before flipping the input** (practice account, 1 lot, Verbose on):

1. Place a bracket and let the stop fill naturally on BOTH sides. The bridge
   must log `twin_gone` (or `twin_gone_on_cancel`), **not** a market. This
   proves the no-double-fill path.
2. Place a resting stop, then drag it through price in Sierra Chart. The
   bridge must log `STILL RESTING ... Firing market` and the portal must go
   flat.
3. Only then set *Close-If-Open On Fill* = Yes on the live chart.

If step 1 ever fires a market, stop and read the ack: TopstepX's open-orders
or cancel semantics differ from the assumption above.

### Brackets and OCO

A bracket in Sierra Chart is three separate orders (entry, stop child, target
child), each with its own id, and each is mirrored independently. Each child's
size is resolved through its parent on every scan, so a child never reads as a
size change and is placed on TopstepX exactly once. OCO works
through the diff loop, not explicit linkage: when Sierra Chart cancels the
sibling on a fill, the next scan sees working -> canceled and emits a cancel.
There is a race window of roughly one scan interval plus bridge poll plus one
REST round-trip in which the TopstepX sibling could fill on its own.

### Tags and replay safety

Every emitted line carries a tag: `manual-{sc_id}-v{N}` for places (N bumps
on each modify), `manual-{sc_id}-cancel` for cancels and
`manual-{sc_id}-v{N}-close` for close-if-open. Tags are
deterministic per Sierra Chart `InternalOrderID`, so the bridge dedupes on
them and replaying an outbox is safe. Tags are **local bookkeeping only** and
are never sent to TopstepX; match portal orders by contract, price and size.

---

## Limitations

Read these before you trust it with size.

- **A stop or limit repriced into the market is only caught with Close-If-Open
  = Yes.** Dragging a stop through price to exit fills in milliseconds and the
  study only ever sees working -> FILLED. With the input off (the default) the
  TopstepX twin keeps its OLD price and the shadow position stays open until
  that price trades. Run the [Close-If-Open](#close-if-open) live test, then
  turn it on.
- **A Sierra Chart limit can fill while the TopstepX twin does not** (queue
  position). Same fix.
- **Partial fills are not mirrored.** The TopstepX twin stays at full size.
- **Cancel + replace is not atomic.** If the re-place fails after the cancel
  succeeds, that resting order is gone on TopstepX. The bridge logs `ERROR`
  loudly. Re-place by hand.
- **The study cannot tell your orders from a trading study's orders.** Sierra
  Chart's order struct carries no such flag. Any automated study trading a
  scanned symbol on the selected account in the same Sierra Chart process is
  mirrored too. See Safety notes.
- **Close-If-Open cannot see a TopstepX partial fill on the twin.** The
  catch-up market is for the full size.
- **No reconcile.** The bridge never reads positions back. The TopstepX
  portal is authoritative; check it.

---

## If something goes wrong mid-session

- **Bridge crashed.** Restart it with the same command. It fast-forwards past
  every outbox line written while it was down (those orders may already be
  cancelled in Sierra Chart) and its `seen_tags.json` prevents double-places.
  Check the portal afterwards.
- **Sierra Chart crashed / study reloaded.** The snapshot is lost. On the next
  scan the bootstrap rule marks every visible order as seen, so nothing is
  re-mirrored. TopstepX orders placed before the crash stay live on their own.
  Flatten the portal by hand if things look inconsistent.
- **Want to stop mirroring NOW.** Flip the study's Enable to No. Do **not**
  kill the bridge process to stop a leak: Sierra Chart keeps appending to the
  outbox and the bridge would resume from its byte cursor on restart. The
  bridge skips lines written while it was off, but the study is the switch.
- **Burst breaker tripped** (`CRITICAL` line in the bridge). More than
  `burstMaxPlaces` order-creating commands arrived within `burstWindowS`.
  Placing halts, cancels still work, and it stays latched until you restart
  the bridge. Check the portal before restarting.

---

## Safety notes

- **Start flat.** Orders already live at Enable time are skipped.
- **Automated studies in the same Sierra Chart instance.** Either keep them in
  a separate Sierra Chart installation, or put them in a different chartbook
  and set *Scope To This Chartbook* = Yes. That scope is fail-closed: an order
  with a blank or mismatched source chartbook is skipped. Prove with a live
  test order and Verbose Logging (`SEE sc_id=... book='...'`) that your
  order-entry path populates the field before you rely on it.
- **Contract roll checklist** (every quarter: March, June, September,
  December). Sierra Chart and TopstepX don't necessarily switch to the new
  month on the same day, so check both sides:
  1. Confirm the month TopstepX is trading now, in the TopstepX platform
     (e.g. `ESZ6`).
  2. Update `contracts:` in the YAML to that month's ids and confirm them with
     contract search (see [step 3](#3-contract-map-and-paths)).
  3. Update the study's *Scan Symbols* input to the same month as it appears
     in your chart header.
  4. Restart the bridge, run `--doctor`, and place the 1-contract test order
     from [Every trading day](#every-trading-day) on a practice account before
     trading size.
- **Switching trade accounts or Sim mode while armed.** The freshness guard
  makes it safe, but the clean habit is Enable -> No, confirm flat, switch,
  re-enable.

---

## Files

```
optd-sierrachart-topstepx-mirror/
├── README.md
├── LICENSE                        MIT
├── requirements.txt               pyyaml, requests
├── start-bridge.bat               double-click: checks setup, then runs the bridge
├── outbox/                        ships empty; the study writes YYYYMMDD.jsonl here
├── sierra/
│   ├── Manual_Mirror.cpp          the study (source of truth; never edit the ACS_Source copy)
│   ├── tsx_manual_emit.h          JSONL emitter used by the study
│   ├── deploy.ps1                 copy both into ACS_Source (Windows)
│   └── deploy.sh                  same, for Git Bash / WSL
└── bridge/
    ├── manual_bridge.py           outbox tail, dispatch, state, --list-accounts
    ├── tsx_client.py              thin TopstepX / ProjectX REST client
    ├── manual_config.example.yaml template -> manual_config.yaml (gitignored)
    └── manual_bridge.example.env  template -> manual_bridge.env  (gitignored)

outbox/YYYYMMDD.jsonl              the study writes, the bridge reads
manual-acks/YYYYMMDD.jsonl         one line per command the bridge processed
manual-state/
    cursor.json                    outbox byte offset for resume
    seen_tags.json                 dedupe table
    sc_to_tsx.json                 sc_id -> TopstepX order id, for cancel/replace
```

---

## Contributing

The ProjectX API is publicly documented, and anyone could build this. It's
published so there is one version people can inspect, test and improve,
instead of another black box. If you find a problem,
[open an issue](https://github.com/drewautomates/optd-sierrachart-topstepx-mirror/issues).
If you can make it better, pull requests are welcome.

---

## License and disclaimer

MIT, copyright (c) 2026 Andrew Thomas (OPTD). See [LICENSE](LICENSE), which
also carries the full risk disclosure. Short version: this software sends real
orders to a real account; it is educational and research software, not
financial advice; you are responsible for every order it sends and for checking
that mirroring is permitted on the accounts you connect.

If you build on this, keeping the copyright notice is required by the license.
A link back to this repo or to
[onepersontradedesk.com](https://onepersontradedesk.com) is appreciated, not
required.
