# NEXTRON — Usage

Every screen, every key, every command.

---

## Installing the command

```bash
./scripts/install-prerequisites.sh
```

That installs the system packages, builds `.venv`, installs NEXTRON into it and
puts a `nextron` launcher in `~/.local/bin`, so the command works from any
directory with no virtual environment to activate. Add `--system` to install the
launcher in `/usr/local/bin` instead, which also makes `sudo nextron` work.

## Launching

```bash
nextron                       # the terminal interface
nextron run --mode vpn_only   # preselect a mode
nextron run -m tor_only -c    # preselect and connect on startup
python -m nextron             # equivalent, from inside the virtual environment
```

For VPN tunnels, the kill switch, transparent routing and DNS on port 53:

```bash
sudo nextron                              # launcher installed with --system
sudo /path/to/nextron/.venv/bin/nextron   # otherwise
```

Running unprivileged is fully supported — NEXTRON reports which capabilities are
unavailable rather than failing silently.

---

## Startup sequence

1. The official banner (`assets/banner.png`) is rendered in the terminal with
   Pillow + Rich. If image rendering is unavailable, the ASCII banner is shown
   automatically.
1. Anything new in `Sources/VPN Profiles` and `Sources/DNS list` is imported.
   Nothing is downloaded, your files are not moved, and a file already in the
   library is recognised by its contents and skipped.
2. The runtime starts: configuration is loaded, the profile library is read, the
   statistics database is opened, and stale firewall rules from a previous
   crashed session are removed.
3. The dashboard opens. Nothing is connected until you ask for it.

Press `Esc`, `Enter` or `Space` to skip the splash immediately.

---

## The dashboard

Three columns:

| Column | Contents |
|---|---|
| Left | Menu, Routing panel, Session panel |
| Middle | Rotation schedulers (both countdowns), Tor Engine, VPN Engine, DNS Shield |
| Right | Live activity log |

### Routing panel

| Row | Meaning |
|---|---|
| Mode / Chain | The selected mode and the path it builds |
| Status | `Idle`, `Starting`, `Verifying`, `Connected`, `Error` |
| Verified | The summary of the last verification pass |
| Reach | `System-wide (TransPort …)` or `SOCKS only — <reason>` |
| Privileges | `root` or `user (sudo for tunnels)` |

`Reach` is the row to read when Tor "works" but your browser is not using it:
in SOCKS mode you must point applications at `127.0.0.1:9050` yourself.

### Tor Engine panel

Status and version, bootstrap percent and phase, exit IP and country, circuit id
and relay path, the SOCKS endpoint, time since the last rotation, and Tor uptime.

### VPN Engine panel

Status and protocol, active profile, tunnel interface and address, the public
address seen from behind the tunnel, country, endpoint, kill switch state,
rotation pool size and time since the last switch.

### DNS Shield panel

Status, listening address, upstream (with `Tor DNSPort` when queries are going
through Tor), number of blocked and whitelisted domains, active list count,
query and block counters with the block rate, and whether system DNS is
redirected.

---

## Global keys

| Key | Action |
|---|---|
| `↑` `↓` | Navigate the focused list |
| `←` `→` | Move focus between panels |
| `Enter` | Select / execute |
| `Space` | Rotate the Tor identity now |
| `R` | Toggle Tor rotation (the Tor scheduler) |
| `F` | Toggle VPN shuffle (the VPN scheduler) |
| `[` `]` | Tor rotation interval down / up the preset ladder |
| `,` `.` | VPN shuffle interval down / up the preset ladder |
| `C` | Connect / disconnect the selected mode |
| `M` | Choose the routing mode (selection only) |
| `V` | VPN Library — choose the active profile |
| `P` | Rotation pool |
| `D` | DNS Shield |
| `X` | Doctor |
| `L` | Logs |
| `S` | Settings |
| `A` | About |
| `I` | Show the last verification report |
| `Q` | Quit (with confirmation) |
| `Esc` | Return from any screen |

---

## Screens

### Rotation intervals

The *Rotation Schedulers* panel is the time-changing element. Each engine shows
its countdown, its current interval and the keys that move it:

```text
      TOR IDENTITY              VPN PROFILE
         01:35                     02:00
      ◂ 1m ▸  [ ]               ◂ 2m ▸  , .
       rotating (R)              Random (F)
```

The ladder is `15s · 30s · 1m · 1m 30s · 2m · 2m 30s · 3m · 3m 30s · 4m ·
4m 30s · 5m`. An arrow disappears at each end, so you can see when you have
reached the shortest or longest step. A hand-edited `config.toml` may still use
any value between 5s and 5m; the steppers snap it onto the ladder on first use.

### Routing Mode (`M`)

Lists the four modes with their chain, summary, full execution sequence, whether
a VPN profile is required and how many are available, plus mode-specific notes.
`Enter` **selects** the highlighted mode and returns to the dashboard — nothing
is started here. Press `C` on the dashboard to connect it, which runs the
verification checklist. Only the selected mode's services run: VPN Only never
starts Tor, Tor Only never touches a tunnel.

### VPN Library (`V`)

| Key | Action |
|---|---|
| `Enter` | **Select this profile as active** (`▸`) — used by the next connection |
| `C` | Connect this profile now |
| `I` | **Import — opens your desktop file browser**, multi-select supported |
| `O` | Import from a typed path (one, or several separated by spaces) |
| `R` | Rename |
| `X` / `Delete` | Delete (with confirmation) |
| `F` | Toggle favourite |
| `A` | Set as the active profile |
| `Enter` | Connect this profile now |
| `U` | Point the profile at a credentials file you created |
| `E` | Export a copy |

Markers: `▸` active, `✦` favourite, `↻` in the rotation pool.

### Rotation Pool (`P`)

| Key | Action |
|---|---|
| `Space` | Add / remove the highlighted profile |
| `A` / `N` | Select every profile / none |
| `G` | Cycle the shuffle algorithm |
| `[` / `]` | Step the interval down / up the preset ladder |
| `I` | Type an exact interval (any value from 5s to 5m) |
| `T` | Start / stop the VPN scheduler |

An empty pool means *every* profile. The footer previews which profile is next.

### DNS Shield (`D`)

| Key | Action |
|---|---|
| `T` | Enable / disable the Shield |
| `U` | **Download every enabled source in the catalogue** |
| `Space` | Enable / disable the highlighted list |
| `I` | **Import — opens your desktop file browser**, multi-select supported |
| `O` | Import from a typed path (one, or several separated by spaces) |
| `X` / `Delete` | Remove a list from the library |
| `W` | Add a domain to the whitelist |
| `R` | Reload every enabled list |

### Doctor (`X`)

Runs the full health report. `R` re-runs it, `O` runs it without network probes.
Each finding that is not OK carries the exact command that fixes it.

### Logs (`L`)

The structured session log, live-updating. `F` cycles the level filter
(All / Info+ / Warnings+ / Errors), `T` toggles following, `C` clears the view
(the log file is untouched). The file path is shown in the header.

### Settings (`S`)

Every persisted option, under the heading of the engine it belongs to:

```
TOR
   Rotation enabled            on
   Rotation interval           1m
   ...
VPN
   Shuffle enabled             off
   ...
DNS SHIELD  /  VERIFICATION  /  INTERFACE
```

`Enter` edits the highlighted row: switches toggle, choices cycle, intervals
step through the preset ladder (wrapping round at 5m), and numbers or lists open
a prompt. Headings are labels — `↑`/`↓` step over them and `Enter` on one does
nothing.

Changes are written to `config.toml` immediately and pushed into the live
engines: changing a rotation interval retimes the running scheduler on the spot.

### About (`A`)

Version, developer, GitHub, email, licence and every local storage path.
`G` opens the repository in your browser (only on that explicit key press),
`E` copies the email address.

---

## Command line

### Sessions

```bash
nextron                                   # interface
nextron connect tor_only                  # headless, holds until Ctrl+C
nextron connect vpn_over_tor -p berlin    # headless with a specific profile
nextron connect vpn_only --shuffle        # force the shuffle scheduler on
```

A headless session prints the verification table, then holds the route open and
tears it down cleanly on `Ctrl+C` — the kill switch is released and
`/etc/resolv.conf` is restored.

### Diagnostics

```bash
nextron doctor              # human readable, exit code 1 if unhealthy
nextron doctor --json       # machine readable
nextron doctor --offline    # skip network probes
nextron ip                  # current public address and location
```

### VPN

```bash
nextron vpn list
nextron vpn import                                   # opens the file browser
nextron vpn import ~/vpn/berlin.ovpn --activate
nextron vpn import ~/vpn/*.ovpn                      # as many as you like
nextron vpn import ~/vpn/ams.wgconf --name "Amsterdam"
nextron vpn activate berlin
nextron vpn pool berlin amsterdam paris   # set the rotation pool
nextron vpn pool                          # clear it (means: every profile)
nextron vpn remove berlin --force
```

Profiles resolve by id, exact name, or unique name prefix.

### DNS

```bash
nextron dns list
nextron dns sources                                  # the catalogue of upstream lists
nextron dns sources --all                            # including disabled ones
nextron dns update                                   # download + convert every source
nextron dns import                                   # opens the file browser
nextron dns import ~/blocklists/StevenBlack.hosts
nextron dns import ~/blocklists/*.hosts              # as many as you like
nextron dns whitelist                     # show the whitelist
nextron dns whitelist example.com         # extend it
nextron dns check ads.example.com         # would this be blocked?
```

### Configuration

```bash
nextron config path      # where everything lives
nextron config show      # the effective configuration as TOML
nextron config reset     # restore defaults (profiles and lists untouched)
```

---

## Environment variables

| Variable | Effect |
|---|---|
| `NEXTRON_HOME` | Relocate the configuration root (default `~/.config/nextron`) |
| `NEXTRON_ASSETS` | Override the asset directory used for the banner |
| `NEXTRON_FORCE_ASCII` | Always use the ASCII banner |
| `NEXTRON_NO_FILE_DIALOG` | Never open the desktop file browser; always prompt for a path |
| `XDG_CONFIG_HOME` | Honoured when `NEXTRON_HOME` is unset |
