```
                                  )
                               ( /(                )
                               )\())   (     )  ( /( (
                              ((_)\   ))\ ( /(  )\()))(    (    (
                               _((_) /((_))\())(_))/(()\   )\   )\ )
                              | \| |(_)) ((_)\ | |_  ((_) ((_) _(_/(
                              | .` |/ -_)\ \ / |  _|| '_|/ _ \| ' \))
                              |_|\_|\___|/_\_\  \__||_|  \___/|_||_|

                        Intelligent Tor Switcher with VPN & Ad Blocking in CLI
```

<div align="center">

**Four routing modes. One keystroke each. Nothing leaves without you knowing.**

![Python](https://img.shields.io/badge/python-3.11%2B-4B0082?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Linux-5D3D94?style=flat-square)
![Interface](https://img.shields.io/badge/interface-100%25%20terminal-9B59B6?style=flat-square)
![Tests](https://img.shields.io/badge/tests-418%20passing-4B0082?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-C8A2C8?style=flat-square)

</div>

---

```
                    ┌──────────────────────────────────────────────┐
     you  ─────────▶│  NEXTRON                                     │
                    │                                              │
                    │    DNS Shield    every lookup, filtered      │
                    │    Router        one of four chains          │
                    │    Kill switch   nftables, armed first       │
                    │    Verifier      proves it, or tears down    │
                    └───────────────────┬──────────────────────────┘
                                        │
                              ╭─────────┴─────────╮
                        Tor ──┤   or  VPN  or     ├── both, either order
                              ╰─────────┬─────────╯
                                        ▼
                                    internet
```

> The same banner NEXTRON prints on launch. In a terminal that can render
> images it shows [`assets/banner.png`](assets/banner.png) instead, and falls
> back to this automatically.

---

## Why this exists

Privacy tooling on Linux tends to arrive in one of two shapes. Either a GUI that
hides what it is doing behind a toggle, or a pile of shell scripts you are
expected to trust because you wrote them at 2am.

NEXTRON is the third shape: a **single keyboard-driven console** that runs the
real daemons — `tor`, `openvpn`, `wg-quick`, `nftables` — reads their actual
output, and then **goes and checks**. It does not tell you that you are
protected. It fetches your exit address, compares it against the one you had
before, tests for DNS and IPv6 leaks, and if any required check fails it takes
the route down rather than leave you believing a lie.

> **Nothing is assumed.** Every status on screen came from a daemon's own
> output, a socket that answered, or a request that returned.

---

## Table of contents

<table>
<tr><td valign="top" width="33%">

**Getting there**
- [Why this exists](#why-this-exists)
- [Routing modes](#routing-modes)
- [Requirements](#requirements)
- [Installation](#installation)
- [First run](#first-run)

</td><td valign="top" width="33%">

**Using it**
- [The interface](#the-interface)
- [Keyboard](#keyboard)
- [The Sources folder](#the-sources-folder)
- [DNS Shield](#dns-shield)
- [Command line](#command-line)

</td><td valign="top" width="34%">

**Under it**
- [Verification](#verification)
- [Privileges](#privileges-what-needs-root-and-why)
- [Architecture](#architecture)
- [Testing](#testing)
- [What it does not do](#what-this-does-not-do)

</td></tr>
</table>

---

## Routing modes

Four chains. Exactly one is live at a time, and switching tears the old one down
before it builds the new one.

<table>
<tr>
<th align="left">Mode</th><th align="left">Chain</th><th align="left">What it buys you</th>
</tr>
<tr>
<td><b>Tor Only</b></td>
<td><code>you → tor → internet</code></td>
<td>Three-hop anonymity. No account, no provider, no bill.</td>
</tr>
<tr>
<td><b>VPN Only</b></td>
<td><code>you → vpn → internet</code></td>
<td>Speed and a stable exit. One party sees everything — the one you chose.</td>
</tr>
<tr>
<td><b>Tor over VPN</b></td>
<td><code>you → vpn → tor → internet</code></td>
<td>Your ISP sees a VPN, not Tor. The guard node sees the VPN, not you.</td>
</tr>
<tr>
<td><b>VPN over Tor</b></td>
<td><code>you → tor → vpn → internet</code></td>
<td>The VPN never learns your address. Needs a <b>TCP</b> OpenVPN profile.</td>
</tr>
</table>

Every mode arms the **nftables kill switch before the tunnel goes up**, not
after. If the tunnel fails to establish, traffic was never able to escape in the
first place.

<details>
<summary><b>How a connect actually unfolds</b> — the sequence NEXTRON follows</summary>

<br>

```
  1  measure the unprotected exit address       so there is something to compare against
  2  arm the kill switch                        nftables, before anything moves
  3  bring the first hop up                     tor bootstrap, or openvpn / wg-quick
  4  wait for the daemon's own ready marker     not a sleep, not a guess
  5  bring the second hop up, if the mode has one
  6  install transparent routing                when running as root
  7  VERIFY ─┬ exit address changed?            required
             ├ Tor confirms the circuit?        required, in Tor modes
             ├ DNS resolving outside the tunnel?
             └ IPv6 escaping?
  8  passed  → dashboard goes live
     failed  → tear down, release the kill switch, show the report
```

</details>

---

## Requirements

| | |
|---|---|
| **OS** | Linux (developed on Debian / Ubuntu / Pop!\_OS derivatives) |
| **Python** | 3.11 or newer |
| **Terminal** | 24-bit colour; 100×30 minimum, wider is better |
| **Daemons** | `tor` · `openvpn` · `wireguard-tools` · `nftables` · `iproute2` |
| **Root** | Only for tunnels, firewall rules and port 53 — see [Privileges](#privileges-what-needs-root-and-why) |

NEXTRON bundles no VPN profile, ships no blocklist, and never phones home. What
it holds is what you put in [`Sources/`](#the-sources-folder).

---

## Installation

### The one-command path

```bash
./scripts/install-prerequisites.sh
```

It checks the distribution, installs the missing system packages, creates the
virtual environment, installs NEXTRON into it, and drops a `nextron` launcher on
your `PATH`. Run it with `--dry-run` first if you would rather read than trust.

### The manual path

```bash
sudo apt install tor openvpn wireguard-tools nftables iproute2 python3-venv
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Confirm the machine is ready

```bash
nextron doctor
```

Every missing piece is reported by name, with the command that fixes it.

---

## First run

```bash
# 1. put your own files where NEXTRON will find them
cp ~/Downloads/*.ovpn  ~/.config/nextron/Sources/'VPN Profiles'/
cp ~/Downloads/ads.txt ~/.config/nextron/Sources/'DNS list'/

# 2. launch
sudo nextron
```

> **Why `sudo`?** Without it NEXTRON still runs, but Tor can only route programs
> pointed at its SOCKS proxy. With it, the whole machine is routed. The Routing
> panel tells you which of the two you have, in words, every time.

Then: **`M`** pick a mode → **`V`** pick a profile → **`C`** connect.

---

## The interface

```
╭─ Menu ──────────────────────────────╮  ╭─ Rotation Schedulers ──────────────╮
│ SESSION                             │  │       TOR IDENTITY    VPN PROFILE  │
│ C   Connect / Disconnect   Connected│  │          04:12           01:38     │
│ M   Routing Mode        Tor over VPN│  │       ◂ 5m ▸  [ ]     ◂ 2m ▸  , .  │
│ R   Tor Rotation                 5m │  │        on (R)          on (F)      │
│ F   VPN Shuffle                  2m │  ╰────────────────────────────────────╯
│                                     │
│ LIBRARIES                           │  ╭─ Tor Engine ───────────────────────╮
│ V   VPN Library         12 profiles │  │ Status      ◉ Connected            │
│ P   Rotation Pool         4 in pool │  │ Bootstrap   100%                   │
│ D   DNS Shield         174k blocked │  │ Exit IP     185.xxx.xxx.xxx        │
│                                     │  │ Path        guard -> middle -> exit│
│ SYSTEM                              │  ╰────────────────────────────────────╯
│ X   Doctor                          │
│ L   Logs                            │  ╭─ Activity ─────────────────────────╮
│ S   Settings                        │  │ 22:41:02 Kill switch armed         │
│ A   About                     1.0.0 │  │ 22:41:09 Tor bootstrapped (100%)   │
╰─────────────────────────────────────╯  │ 22:41:11 Verified: exit changed    │
```

Three columns that stay in step at any terminal size. Click an entry or press
its letter — they do exactly the same thing.

### Reading the status markers

The marker beside a status tells you what is happening without reading the word
next to it.

| | |
|---|---|
| `⠋ ⠙ ⠹ ⠸ …` | **spinning** — coming up, bootstrapping, verifying, rotating |
| `◉ ◍ ◌` | **breathing** — up and carrying traffic |
| `◌` | idle, or disabled |
| `✗` | failed |

Only a service actually in use moves. Motion on screen always means *something
is happening right now* — and an idle dashboard repaints once a second, exactly
as it would with no animation at all.

---

## Keyboard

<table>
<tr><td valign="top">

**Session**

| Key | |
|---|---|
| `C` | Connect / disconnect |
| `M` | Choose the routing mode |
| `Space` | New Tor identity, now |
| `R` | Tor rotation on / off |
| `F` | VPN shuffle on / off |
| `[` `]` | Tor interval − / + |
| `,` `.` | VPN interval − / + |

</td><td valign="top">

**Libraries and system**

| Key | |
|---|---|
| `V` | VPN Library |
| `P` | Rotation Pool |
| `D` | DNS Shield |
| `X` | Doctor |
| `L` | Logs |
| `S` | Settings |
| `A` · `I` | About · last report |
| `Esc` · `Q` | Back · quit |

</td></tr>
</table>

**`M` and `V` select; they never connect.** Choosing a mode or a profile changes
what `C` will do — it does not do it. Connecting stays one deliberate keystroke,
so nothing tears down a live route while you are browsing a list.

Intervals walk a ladder rather than asking you to type a number:
**15s → 30s → 1m → 1m30s → … → 5m**.

---

## The Sources folder

Two folders, managed with your own file manager. **They are the library** —
NEXTRON mirrors them rather than importing from them.

```
~/.config/nextron/Sources/
├── VPN Profiles/     .ovpn  .conf  .wgconf  .json
└── DNS list/         .txt   .hosts  .list
```

| You do this | NEXTRON does this |
|---|---|
| drop a file in | imports it on the next scan |
| edit a file already there | re-reads it |
| **delete a file** | **removes it — and its stored copy** |
| empty the folder | the library is empty |

So what you see in the interface is exactly what is in those two folders. There
is never a copy left behind somewhere you cannot find and delete.

Scanned at startup and every time you open `V` or `D`, so a file dropped in
while NEXTRON is running appears without a restart. Two deliberate exceptions: a
profile that is **connected right now** survives until you disconnect rather
than having its configuration pulled out from under a live tunnel, and lists
**downloaded with `U`** are NEXTRON's own. Importing from anywhere else (`I`, or
`nextron vpn import`) copies the file into `Sources/` first, so the folder and
the library can never disagree.

---

## DNS Shield

A DNS resolver of NEXTRON's own, in front of everything. It answers from a
blocked-domain set, forwards the rest through Tor's DNSPort when Tor is up, and
leaves `/etc/resolv.conf` exactly as it found it when you stop.

NEXTRON ships **no lists**. Drop your own into `Sources/DNS list`, and it works
out what kind of file you gave it:

| What is in the file | What happens |
|---|---|
| domains, `\|\|ads.example.com^` rules, or a hosts file | imported, enabled, converted to blocked domains |
| one `https://…` per line | treated as a **download catalogue** — press `D` then `U` to fetch what it points at |

That distinction matters: a catalogue imported as a blocklist would block
precisely nothing, and would sit there looking like it had worked.

<details>
<summary><b>What conversion can and cannot express</b></summary>

<br>

Filter lists are written for a browser extension that sees whole URLs, request
types and the originating page. A resolver sees a name and nothing else:

| Rule | Result |
|---|---|
| `\|\|ads.example.com^` | blocked |
| `\|\|ads.example.com^$third-party` | blocked |
| `@@\|\|allowed.example.com^` | allowed — global exception |
| `\|\|example.com^$domain=other.tv` | skipped — scoped to one site |
| `example.com##.banner` | skipped — cosmetic |
| `\|\|example.com/ads.js` | skipped — path |

Skipped rules are counted and shown per list, so a cosmetic-only list reads as
*"nothing here for DNS to enforce"* rather than as a failure.

</details>

---

## Command line

Everything the interface does, scriptable.

```bash
nextron                       # the console
nextron connect tor_only      # headless: hold a mode until Ctrl+C
nextron doctor                # system health report
nextron ip                    # public address and country, as seen right now

nextron vpn    list | import | remove | activate | pool
nextron dns    list | import | sources | update | whitelist | check
nextron config path | show | reset
```

---

## Verification

A connection is not "up" because a process started. After every connect:

| Check | Required | What it means |
|---|---|---|
| **Exit address changed** | yes | Measured before, measured after, compared |
| **Tor confirms the circuit** | yes, in Tor modes | Asked over the ControlPort, not inferred |
| **DNS leak** | warning | Are lookups escaping the tunnel? |
| **IPv6 leak** | warning | Is v6 traffic bypassing a v4-only tunnel? |
| **Reach** | informational | System-wide, or SOCKS clients only |

A failed *required* check tears the route down. Press `I` for the full report of
the last attempt, whether it passed or failed.

---

## Privileges: what needs root and why

NEXTRON runs unprivileged and tells you what it cannot do. It never quietly
escalates.

| Operation | Needs root | Because |
|---|---|---|
| Tor SOCKS proxy | no | A user-space port |
| Reading state, libraries, logs | no | All under `~/.config/nextron` |
| OpenVPN / WireGuard tunnels | **yes** | Creating a `tun` / `wg` device |
| nftables kill switch | **yes** | Firewall tables |
| System-wide transparent routing | **yes** | NAT redirect rules |
| DNS Shield on port 53 | **yes** | Privileged port — falls back to 5353 |

Tor itself is dropped to the `debian-tor` user even when NEXTRON is root, and
anything created during a `sudo` run is handed back to the invoking user on the
way out, so the next unprivileged run is not locked out of its own files.

---

## Architecture

```
nextron/
├── core/           config · events · runtime · process · verification
├── tor/            daemon supervision · ControlPort · circuits · NEWNYM
├── vpn/            OpenVPN · WireGuard · profiles · shuffle · kill switch
├── dns/            resolver · blocklists · filter-list conversion · sources
├── routing/        the four modes · transparent proxy · nftables
├── scheduler/      Tor identity rotation · VPN profile rotation
├── state/          one snapshot every screen reads from
├── storage/        paths · sqlite · the Sources mirror
├── diagnostics/    doctor · leak probes
└── tui/            screens · widgets · layout · theme
```

Roughly **15,800 lines** of package code and **5,100** of tests. Every engine is
async and non-blocking: the interface keeps painting while Tor bootstraps.

Three rules the codebase holds to:

1. **One state object.** Engines publish snapshots; screens render them. No
   screen reaches into an engine, and no engine knows a screen exists.
2. **No guessed status.** If it is on screen, a daemon said it, a socket
   answered it, or a request returned it.
3. **Teardown is as tested as setup.** Every rule installed has a test that it
   comes back off — including after a crash.

---

## Testing

```bash
.venv/bin/python -m pytest      # 418 tests, no root and no live tunnels needed
.venv/bin/python -m ruff check nextron tests
```

The suite drives the real Textual interface through its test pilot — pressing
keys, clicking rows, reading what is actually on screen. A few of its more
pointed members, and the bug each one exists because of:

| Test | Why it is there |
|---|---|
| `test_openvpn_output_reaches_the_reader_and_the_log` | `--log-append` sent OpenVPN's stdout to a file, so the ready marker never arrived and *every* tunnel timed out |
| `test_a_profile_that_wants_a_password_is_refused_before_openvpn_starts` | A bare `auth-user-pass` handed the prompt to systemd and hung for minutes |
| `test_the_interface_draws_only_one_cell_glyphs` | Ambiguous-width symbols shifted a row and made the panel borders stagger |
| `test_no_screen_method_is_shadowed_by_a_textual_attribute` | `Widget._animate` silently replaced a method of the same name |
| `test_vpn_only_never_starts_tor` | Selecting a mode used to connect it on the spot |
| `test_removing_a_file_removes_the_profile` | Deleting a file from `Sources/` left the profile behind |

---

## What this does not do

Worth being plain about, because privacy tools attract wishful reading.

- **It is not anonymity by itself.** Tor protects the transport. A logged-in
  browser, a fingerprintable one, or a document that phones home defeats it.
- **It does not vet your VPN.** A provider sees your traffic. NEXTRON routes to
  the one you chose; choosing well is yours.
- **DNS blocking is not ad blocking.** Names resolve to nothing; same-origin ads
  and cosmetic clutter still need a browser extension.
- **A kill switch is not a guarantee.** It is an nftables ruleset. Root can
  remove it, and so can a crash it cannot catch — which is why NEXTRON clears
  stale rules on the next start.

---

## Documentation

| | |
|---|---|
| [`docs/USAGE.md`](docs/USAGE.md) | Every screen, every key |
| [`docs/ROUTING_MODES.md`](docs/ROUTING_MODES.md) | The four chains, and when each is the right one |
| [`docs/DNS_SHIELD.md`](docs/DNS_SHIELD.md) | Resolver, blocklists, conversion |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Every setting in `config.toml` |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the pieces fit |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | When it does not work |

---

## License

[MIT](LICENSE). Use it, fork it, ship it.

<div align="center">
<sub>Built for people who would rather see the machinery than be reassured about it.</sub>
</div>
