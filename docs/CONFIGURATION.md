# NEXTRON — Configuration

Everything lives in `~/.config/nextron/config.toml`. It is written on first
launch, validated on every load, and safe to edit by hand — invalid values are
clamped or replaced with defaults rather than crashing the application.

The Settings screen (`S`) edits every key below and writes the file immediately.

```bash
nextron config show     # the effective configuration
nextron config path     # where everything lives
nextron config reset    # restore defaults (profiles and lists untouched)
```

---

## Top level

```toml
version = "1.0.0"
routing_mode = "tor_only"   # tor_only | vpn_only | tor_over_vpn | vpn_over_tor
log_level = "INFO"          # DEBUG | INFO | WARNING | ERROR | CRITICAL
```

An unrecognised `log_level` falls back to `INFO`.

---

## `[tor]`

```toml
enabled = true
socks_port = 9050
control_port = 9051
dns_port = 9053
trans_port = 9040

manage_daemon = true          # launch a private tor process
binary = "tor"
bootstrap_timeout = 120       # 15-600 seconds without bootstrap *progress*

transparent_routing = true    # system-wide redirect (needs root + a system daemon)

rotation_enabled = true
rotation_interval = 60        # 5-300 seconds, hard clamped
verify_exit_after_rotation = true
rotation_retries = 3          # 0-10 exit-address polls after NEWNYM

exit_countries = []           # e.g. ["DE", "NL"]
strict_exit_nodes = false     # never leave the chosen countries
```

Notes:

- `socks_port`, `control_port`, `dns_port` and `trans_port` are *preferences*.
  When a port is already taken, NEXTRON starts its daemon on a nearby free one
  (9050 → 9250) and reports the move; the file is not rewritten.
- `bootstrap_timeout` is a **stall** timeout: it limits the gap between progress
  reports, not the total bootstrap time. A slow connection that keeps making
  progress is allowed to finish; a genuinely stuck daemon fails promptly.

- `rotation_interval` is clamped to **5–300 seconds** in every code path
  (file, Settings screen, CLI) — the specification's 5s–5m window.
- The interface's steppers walk a fixed ladder inside that window:
  `15s, 30s, 1m, 1m30s, 2m, 2m30s, 3m, 3m30s, 4m, 4m30s, 5m`. A value set by
  hand (say `45`) is kept as-is and snapped onto the ladder the first time you
  press a stepper key.
- `manage_daemon = false` attaches to a Tor daemon someone else started. This is
  what you want for system-wide transparent routing, because the daemon then
  runs as `debian-tor` and can be exempted from the redirect.
- If a `ControlPort` is already serving when NEXTRON starts, it attaches to that
  daemon instead of launching a second one, whatever `manage_daemon` says.
- `exit_countries` is passed to Tor as `ExitNodes`; with `strict_exit_nodes` it
  becomes `StrictNodes 1`, which can leave you unable to connect if the chosen
  countries are unreachable.

---

## `[vpn]`

```toml
active_profile = ""           # profile id; set with `nextron vpn activate`
openvpn_binary = "openvpn"
wireguard_binary = "wg-quick"
wg_tool_binary = "wg"
connect_timeout = 60          # 10-300 seconds

shuffle_enabled = false
shuffle_algorithm = "random"  # random | sequential | round_robin | no_repeat
shuffle_interval = 120        # 5-300 seconds, hard clamped
shuffle_pool = []             # profile ids; empty means every profile

killswitch_enabled = true
verify_tunnel = true          # read the public address after connecting
reconnect_retries = 3         # 0-10 attempts per connect
```

`shuffle_pool` holds profile ids, not names. Manage it with the Rotation Pool
screen (`P`) or `nextron vpn pool <names…>`, which resolves names for you.

---

## `[dns]`

```toml
enabled = false
listen_host = "127.0.0.1"
listen_port = 53
fallback_port = 5353
upstream = ["1.1.1.1", "9.9.9.9"]
prefer_tor_dns = true
block_ads = true
block_trackers = true
block_malware = true
enabled_blocklists = []
manage_resolv_conf = true
cache_ttl = 300               # 0-86400 seconds; 0 disables caching
block_ipv6_answers = false
```

`enabled_blocklists` is maintained for you: `nextron dns update` adds every list
it writes. The upstream catalogue lives beside the lists in
`~/.config/nextron/dns/sources.txt` and is plain text — one URL per line, `#` to
disable one.

See [DNS_SHIELD.md](DNS_SHIELD.md) for what each key changes in practice.

---

## `[verification]`

```toml
enabled = true
check_dns_leak = true
check_ipv6_leak = true
check_routing = true
strict = true                 # refuse a mode if any required check fails
timeout = 20.0                # 5-120 seconds per network probe
```

With `strict = true` (the default) a required failure tears the route back down
rather than leaving you connected to something you did not ask for. Leak checks
are always reported as warnings, so they never trigger a teardown.

Setting `enabled = false` makes every mode report *Connected* as soon as the
sequence finishes. The verification row then reads `skipped` — it never claims a
check passed that was not run.

---

## `[interface]`

```toml
show_png_banner = true        # ASCII is still used automatically on failure
banner_width = 72             # 24-200 cells
splash_seconds = 2.0          # 0-10; 0 shows the splash until a key press
activity_log_lines = 500      # 50-5000
confirm_quit = true
```

---

## Environment variables

| Variable | Effect |
|---|---|
| `NEXTRON_HOME` | Relocate the entire configuration root |
| `NEXTRON_ASSETS` | Override the asset directory used for the banner |
| `NEXTRON_FORCE_ASCII` | Always use the ASCII banner |
| `NEXTRON_NO_FILE_DIALOG` | Never open the desktop file browser; always prompt for a path |
| `NEXTRON_ASSETS` | Where the banner artwork is read from |
| `XDG_CONFIG_HOME` | Honoured when `NEXTRON_HOME` is unset |

`NEXTRON_HOME` is the clean way to keep separate configurations:

```bash
NEXTRON_HOME=~/.config/nextron-work nextron
```

---

## Validation behaviour

| Situation | Result |
|---|---|
| Unparseable TOML | Defaults are used; the file is left untouched so you can fix it |
| A key with the wrong type | Defaults are used for the whole document; the error count is logged |
| An unknown key | Ignored |
| An out-of-range number | Clamped to the nearest valid value |
| A missing file | Written with defaults |

Writes are atomic (temp file plus rename), so an interrupted save cannot leave a
truncated configuration behind.
