# NEXTRON — Architecture

```text
                         NEXTRON
                Keyboard-first Textual TUI
                            │
           ┌────────────────┼────────────────┐
           ▼                ▼                ▼
      Tor Engine       VPN Engine       DNS Shield
   (daemon + Stem)  (OpenVPN/WireGuard)  (resolver +
           │         + kill switch)       blocklists)
           └────────────────┼────────────────┘
                            ▼
                     Routing Manager
                            │
                   Verification Engine
                            │
                         Internet
```

---

## Module map

```text
nextron/
├── __init__.py            package identity and version
├── __main__.py            python -m nextron
├── cli.py                 Typer CLI: run, connect, doctor, vpn, dns, config
│
├── core/
│   ├── constants.py       identity, brand palette, ports, rotation bounds
│   ├── exceptions.py      NextronError hierarchy
│   ├── config.py          Pydantic models + TOML persistence + clamping
│   ├── events.py          async fan-out event bus
│   ├── context.py         EngineContext: config + bus + state + database
│   ├── process.py         async subprocess helpers, sudo, graceful termination
│   ├── verification.py    the eight-check verification engine
│   └── runtime.py         NextronRuntime — owns every engine
│
├── tor/
│   ├── daemon.py          torrc generation, daemon supervision, bootstrap parsing
│   ├── controller.py      Stem ControlPort (async facade over a sync library)
│   └── engine.py          rotation, exit verification, circuit refresh
│
├── vpn/
│   ├── certs.py           best-effort expiry of an inline client certificate
│   ├── profiles.py        profile library, protocol detection, import formats
│   ├── openvpn.py         OpenVPN backend, runtime config, SOCKS injection
│   ├── wireguard.py       WireGuard backend via wg-quick / wg
│   ├── killswitch.py      nftables (iptables fallback) default-drop egress
│   ├── shuffle.py         Random / Sequential / Round Robin / No Repeat
│   └── manager.py         VPNEngine: connect, switch, watchdog, verify
│
├── dns/
│   ├── adblock.py         filter-list syntax -> the domains DNS can enforce
│   ├── blocklist.py       .txt / .hosts / .list parsing, whitelist, matching
│   ├── resolver.py        asyncio DNS server, sinkholing, cache, forwarding
│   ├── sources.py         the upstream catalogue and its downloader
│   └── shield.py          orchestration, upstream selection, resolv.conf
│
├── routing/
│   ├── modes.py           descriptors: chain, sequence, requirements, notes
│   ├── transparent.py     Tor TransPort/DNSPort NAT redirect
│   └── manager.py         per-mode sequences, verification, ordered teardown
│
├── scheduler/
│   ├── base.py            IntervalScheduler: clamping, countdown, retry, pause
│   ├── tor_scheduler.py   NEWNYM on a timer
│   └── vpn_scheduler.py   profile shuffling on a separate timer
│
├── state/
│   └── manager.py         immutable AppState snapshots + watchers
│
├── diagnostics/
│   ├── doctor.py          the health report
│   └── leaks.py           DNS-leak and IPv6-leak probes (shared with verification)
│
├── storage/
│   ├── paths.py           the filesystem layout
│   └── database.py        aiosqlite statistics store
│
├── utils/
│   ├── banner.py          PNG half-block rendering + ASCII fallback
│   ├── net.py             public IP, geo, Tor check, port and DNS probes
│   ├── logging.py         rotating file log + in-memory ring buffer
│   ├── intervals.py       the rotation preset ladder and its stepper
│   ├── filedialog.py      native file chooser (zenity / kdialog / yad)
│   └── format.py          durations, countdowns, truncation
│
└── tui/
    ├── app.py             NextronApp, workers, guaranteed teardown
    ├── themes/            the brand palette as a Textual theme + stylesheet
    ├── layouts/           panel registry shared by compose and refresh
    ├── widgets/           banner, panels, countdowns, activity log, menu
    └── screens/           splash, dashboard, mode select, VPN library,
                           rotation pool, DNS Shield, doctor, logs, settings,
                           about, modals

scripts/
└── install-prerequisites.sh   packages, virtualenv, and the `nextron` launcher
```

---

## Design decisions

### One runtime, two front ends

`NextronRuntime` owns configuration, the event bus, the state manager, the
database, all three engines, the routing manager and both schedulers. The TUI and
the CLI are thin drivers over it, so `nextron connect tor_only` and pressing
`C` on the dashboard execute exactly the same code.

### Engines never touch the interface

Engines mutate `StateManager` and publish events. The interface subscribes.
Consequences worth having:

- the TUI can be replaced, or absent (headless mode), without touching engines;
- a broken widget cannot take an engine down — subscriber exceptions are caught
  and logged;
- the activity log is just an event subscriber, so nothing has to be threaded
  through the call stack to become visible.

### Immutable state snapshots

`AppState` and its sub-states are frozen dataclasses. Every mutation produces a
new snapshot through `dataclasses.replace`, so a widget holding an old snapshot
cannot observe a half-applied update, and `state.tor.exit_ip` is always
consistent with `state.tor.circuit_id`.

### Async everywhere, blocking libraries isolated

The whole core is `asyncio`. Stem is synchronous, so every Stem call goes through
`asyncio.to_thread`, and Tor's asynchronous events are marshalled back onto the
loop with `call_soon_threadsafe`. Blocklist parsing — potentially a million lines
— runs in a thread too. Nothing blocks the render loop.

### Real binaries, observed rather than assumed

No protocol is re-implemented. OpenVPN's stdout is followed until it prints
`Initialization Sequence Completed`, and the tunnel device and address are read
from its own log lines. `wg-quick up` is followed by confirming the interface
actually exists in the kernel. Tor's bootstrap percentage comes from its notice
log and from `status/bootstrap-phase`.

### The desktop is asked for a file browser

NEXTRON does not draw a file picker of its own. The import screens shell out to
the desktop's chooser (`zenity`, `kdialog`, `yad`, `qarma`) and parse the paths
it prints, which gives multiple selection, bookmarks, previews and typed paths
for free. When there is no graphical session the chooser is reported as
unavailable and the screen falls back to a typed-path prompt; a chooser that
could not reach the display (the common `sudo` case) is distinguished from the
user simply cancelling, because only one of those deserves a message.

### Lists are catalogued, not vendored

Bundling snapshots of 135 third-party filter lists would ship stale data under
other people's licences and bloat the repository. NEXTRON ships the addresses
and converts on demand, which keeps provenance obvious (every downloaded file
carries a `# source:` header) and updates one command away.

### Honest degradation

Capabilities that need root are checked, and their absence is reported as an
absence. The kill switch reports `unavailable` rather than `armed`; transparent
routing reports `SOCKS only — <reason>`; the DNS Shield says which port it landed
on. The rule throughout: never claim protection that is not being provided.

### Teardown is guaranteed

`run_app()` runs the Textual app inside `asyncio.run` with the runtime shutdown in
a `finally`, on the same loop that spawned the child processes. An orderly quit,
`Ctrl+C` and an unhandled exception all release the kill switch, restore
`/etc/resolv.conf`, remove the NAT rules and stop the Tor and VPN processes.
Rules left behind by a hard crash are detected and removed at the next launch.

---

## Data flow: establishing a mode

```text
key press / CLI argument
        │
        ▼
NextronRuntime.connect(mode)
        │
        ▼
RoutingManager.establish(mode)
        │
        ├─ capture the unprotected baseline address
        ├─ run the mode's sequence (VPN and/or Tor, in the right order)
        ├─ engage transparent routing or expose SOCKS
        └─ start the DNS Shield if enabled
        │
        ▼
VerificationEngine.verify(mode)          8 checks, one event each
        │
        ├─ passed ──► route_status = ACTIVE ─► ROUTE_UP ─► schedulers start
        └─ failed ──► ordered teardown ─────► RoutingError with the reason
```

Every step publishes an event, so the activity log is a faithful transcript of
what actually happened, and the statistics database records the outcome.

---

## Testing strategy

The suite runs without root, without live tunnels and without the `tor` binary:

| Area | Approach |
|---|---|
| Configuration | Round-trip TOML, clamping, corrupt and unknown keys |
| Shuffle | All four algorithms with a seeded RNG, pool changes, peek |
| Blocklists | All three formats, subdomain matching, whitelist precedence |
| Resolver | A stubbed upstream: sinkholing, caching, transaction ids, SERVFAIL |
| Schedulers | Real timers at short intervals: countdown, pause, trigger, retry |
| Rulesets | Pure generation functions asserted as text |
| Verification | Fake engines and patched probes, per-mode applicability |
| Banner | PNG path, forced ASCII, missing asset, corrupt PNG |
| Database | A real SQLite file under a temporary `NEXTRON_HOME` |
| Filter conversion | Every rule shape: hosts, `||domain^`, modifiers, exceptions, cosmetic, regex |
| Catalogue | Parsing, de-duplication, enable/disable round-trip |
| Downloader | Mock transport: success, cosmetic-only, HTTP errors, broken encodings, progress |
| Sources drop-box | Import on startup, dedupe by contents, catalogue vs blocklist detection |
| Intervals | Ladder arithmetic: snapping, stepping, end stops, wrapping |
| File chooser | Backend detection, per-backend command lines, output parsing, cancel vs failure |
| Interface | Textual's headless pilot walks every screen, steps the intervals, imports files, and asserts the selection highlight is actually dark |

Network-dependent probes are patched, never called, so the suite is
deterministic offline.
