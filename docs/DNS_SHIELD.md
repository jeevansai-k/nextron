# NEXTRON — DNS Shield

DNS Shield is **not** a routing mode. It is an independent protection layer that
works with all four modes and can be toggled at any time with `D`.

---

## What it does

A small asyncio DNS server (UDP and TCP) that answers every query itself:

| Query | Action |
|---|---|
| A blocked name | **Sinkholed immediately** — `0.0.0.0` for A, `::` for AAAA, `NXDOMAIN` for other types. No upstream query is ever sent, so a blocked tracker is never told you looked it up. |
| Anything else | Forwarded to the upstream, then cached with a bounded TTL. |

### Upstream selection

| Situation | Upstream |
|---|---|
| Tor is running and `prefer_tor_dns` is on | `127.0.0.1:<tor.dns_port>` — Tor's own `DNSPort` |
| Otherwise | The resolvers in `dns.upstream` (default `1.1.1.1`, `9.9.9.9`) |

Sending queries through Tor's `DNSPort` is what stops DNS escaping around the
tunnel. The dashboard shows `Tor DNSPort` in the *Upstream* row when this is
active, and the DNS-leak check passes on that basis.

### System-wide protection

When NEXTRON has root and the Shield is on port 53, `/etc/resolv.conf` is
rewritten to point at the Shield. The original is copied to
`~/.config/nextron/runtime/resolv.conf.nextron-backup` and restored on exit —
including on `Ctrl+C` and on an unhandled error.

Without root:

- the listener falls back to `dns.fallback_port` (default 5353);
- `/etc/resolv.conf` is left alone;
- NEXTRON says so in the activity log. Applications you point at the Shield are
  protected; the rest of the system is not.

---

## Blocklists

### The download catalogue

NEXTRON ships no filter lists and no catalogue. Drop a file of `https://…`
addresses into `Sources/DNS list` and it becomes the catalogue at
`~/.config/nextron/dns/sources.txt` (a file of *domains* is imported as a
blocklist instead — NEXTRON tells them apart by their contents). Downloading is
always an explicit act:

```bash
nextron dns sources        # what is in the catalogue
nextron dns update         # download and convert every enabled source
```

or press `U` on the DNS Shield screen. A full update of a 135-entry catalogue
takes about 45 seconds and produces roughly 174,000 blocked domains from the
~67 lists that convert usefully.

Each downloaded list is converted from filter-list syntax into the plain domains
a resolver can enforce, and written with a `# source:` header recording where it
came from. Rules that DNS cannot express — cosmetic (`##`), path-based and
site-scoped ones — are counted and reported per list, not silently dropped. A
list made entirely of such rules is reported as *cosmetic-only*, which is an
outcome, not a failure.

Disabled catalogue entries carry the reason they were disabled (`unreachable:
HTTP 404`, `cosmetic-only list`). Uncomment any line to try it again.

### Importing your own lists

Press `I` on the DNS Shield screen and your desktop's file browser opens,
filtered to `.txt`, `.hosts` and `.list`, with multiple selection — import a
whole folder of lists in one go. `O` imports a typed path instead.

From the command line:

```bash
nextron dns import                                # opens the file browser
nextron dns import ~/Downloads/StevenBlack.hosts
nextron dns import ~/blocklists/*.hosts           # as many as you like
```

Files dropped straight into `~/.config/nextron/dns/` are discovered too.

Three formats, freely mixed:

| Format | Accepted lines |
|---|---|
| `.hosts` | `0.0.0.0 ads.example.com`, `127.0.0.1 ads.example.com` |
| `.txt` | `ads.example.com`, `\|\|ads.example.com^`, `# comment`, `! comment` |
| `.list` | same as `.txt` |

Malformed lines are counted and ignored, not silently dropped — the DNS Shield
screen shows an `Invalid` column per list so a broken download is visible.

`localhost`, `localhost.localdomain` and `local` are never blocked, whatever a
list says.

### Matching

Matching walks the label chain, most specific first:

```text
a.b.ads.example.com  →  a.b.ads.example.com
                        b.ads.example.com
                        ads.example.com
                        example.com
```

So one `ads.example.com` entry blocks every subdomain beneath it. Single-label
names (`com`) are excluded, so one stray TLD entry in a list cannot break the
internet.

### Whitelist precedence

The whitelist is evaluated over the **whole chain before** the blocklists.
Whitelisting `example.com` therefore releases `ads.example.com` even though a
list blocks it explicitly. This is the behaviour you want when a site you need
is broken by an aggressive list.

```bash
nextron dns whitelist example.com     # add
nextron dns whitelist                 # show
nextron dns check ads.example.com     # would this be blocked right now?
```

The whitelist lives in `~/.config/nextron/whitelist.txt`, one domain per line,
`#` comments allowed.

---

## Managing lists from the interface

Press `D` on the dashboard:

| Key | Action |
|---|---|
| `T` | Enable / disable the Shield |
| `U` | Download every enabled source in the catalogue |
| `Space` | Enable / disable the highlighted list |
| `I` | Import — opens your desktop file browser (multi-select) |
| `O` | Import from a typed path |
| `X` / `Delete` | Remove a list from the library |
| `W` | Whitelist a domain |
| `R` | Reload every enabled list |

Enabling and disabling is persisted in `dns.enabled_blocklists`, so the same set
comes back on the next launch. Toggling a list reloads the merged domain set and
clears the resolver cache immediately.

---

## Configuration keys

```toml
[dns]
enabled = false                  # start the Shield with the routing mode
listen_host = "127.0.0.1"
listen_port = 53                 # needs root
fallback_port = 5353             # used when 53 is unavailable
upstream = ["1.1.1.1", "9.9.9.9"]
prefer_tor_dns = true            # use Tor's DNSPort whenever Tor is running
block_ads = true
block_trackers = true
block_malware = true
enabled_blocklists = []          # file names in dns/ that are active
manage_resolv_conf = true        # redirect system DNS when root
cache_ttl = 300                  # seconds; 0 disables caching
block_ipv6_answers = false       # deny AAAA to close an IPv6 leak path
```

`block_ipv6_answers` is worth knowing about: on a dual-stack network, denying
AAAA keeps traffic on an IPv4-only tunnel and closes the most common IPv6 leak
without touching your kernel settings.

---

## Statistics

Counters are shown live on the dashboard (queries, blocked, block rate) and are
flushed into `~/.config/nextron/stats.db` every 30 seconds, aggregated per day.
Nothing is transmitted anywhere — the database exists so that after a restart you
can still answer "how much did this actually block".

---

## Recommended lists

NEXTRON ships no blocklists — you choose your own. Widely used, freely available
options include the StevenBlack unified hosts file, the OISD lists, and the Peter
Lowe list. Any file in `.txt`, `.hosts` or `.list` format works; download it with
your own tooling and import it.
