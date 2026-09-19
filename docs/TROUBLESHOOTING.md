# NEXTRON — Troubleshooting

Start here:

```bash
nextron doctor
```

Every finding that is not OK carries the exact command that fixes it. The
sections below cover the cases where the symptom and the cause are not obviously
related.

---

## Tor

### "The 'tor' daemon is not installed"

```bash
sudo apt install tor
```

NEXTRON launches its own private daemon by default and does not need
`tor.service` to be running.

### "Could not bind to 127.0.0.1:9050: Address already in use"

You should not see this any more — but this is what it meant. Installing the
`tor` package also *starts* it, and the distribution's `tor.service` listens on
9050 while leaving its ControlPort switched off. A second daemon cannot share
that port, and a daemon without a ControlPort cannot be told to rotate.

NEXTRON now detects the occupied port before launching and moves its own
listeners out of the way, logging:

```text
Another Tor is already listening; NEXTRON moved its own daemon: SOCKS 9050→9250
```

Everything works; the only difference is the port. **Point your applications at
the SOCKS port shown on the dashboard** (`Tor Engine` → `SOCKS`), not at 9050 —
9050 is the system daemon, which NEXTRON is not driving.

`nextron doctor` reports the situation before you connect.

### I would rather use the system daemon than a second one

Then it needs a ControlPort, and your user needs to be allowed to read its
cookie:

```bash
printf 'ControlPort 9051\nCookieAuthentication 1\n' | sudo tee -a /etc/tor/torrc
sudo systemctl restart tor
sudo usermod -aG debian-tor "$USER"      # then log out and back in
```

Then in Settings (`S`) → Tor → **Manage own daemon** → off. NEXTRON will attach
to the running daemon on 9050 instead of starting its own. This is also what
system-wide transparent routing requires.

### "Tor stopped making progress for 120s"

The bootstrap stalled rather than merely being slow — NEXTRON allows as long as
tor keeps reporting progress, and only gives up when it goes quiet. The message
says how far it got:

* stuck below 10% — tor cannot reach the network at all. A firewall, captive
  portal or filtered connection is the usual cause.
* stuck higher up — usually a slow directory fetch. Try again, or raise
  *Bootstrap timeout* in Settings.

### "The Tor daemon exited immediately"

The message quotes tor's own first error, which names the cause. The generated
configuration is at `~/.config/nextron/runtime/torrc` if you want to inspect it.

### Bootstrap stalls below 100%

The dashboard shows the exact phase it stalled on.

- `Connecting to a relay` — a firewall or captive portal is blocking Tor. Try
  another network.
- `Loading relay descriptors` and stuck — usually time-related. Check the clock:
  `timedatectl status`.
- Raise the limit in Settings (`S`) → Tor → *Bootstrap timeout* if the connection
  is simply slow.

### Tor is "Connected" but my browser's IP has not changed

Read the **Reach** row in the Routing panel. It says which of the two you have.

`SOCKS only → 127.0.0.1:PORT`
: Only applications pointed at that proxy use Tor. The browser is untouched and
  keeps its real address — that is not a fault, it is what SOCKS mode means.
  Either configure the browser (SOCKS5, that host and port, tick *proxy DNS*),
  or use the next option.

`System-wide (TransPort …)`
: Everything on the machine exits through Tor, browser included. To get it:

  ```bash
  cd /path/to/nextron && sudo .venv/bin/nextron
  ```

  With root, NEXTRON runs its daemon as `debian-tor` and redirects all outbound
  TCP and DNS into it, dropping IPv6 and QUIC so nothing slips past. Verify with
  `nextron ip`, which shows the direct and through-Tor addresses side by side.

If the row still says SOCKS only under sudo, the reason is printed beside it —
the usual one is a missing system Tor account to borrow the uid from
(`sudo apt install tor`).

### The circuit shows "--" just after connecting

The dashboard reports a circuit only once tor has a real three-hop one. The
one-hop circuits tor uses to fetch directory information are deliberately not
shown, because presenting one as your path through the network would be
misleading. It fills in within a few seconds.

### The exit address does not change after a rotation

Expected occasionally. Tor coalesces `NEWNYM` signals sent within ten seconds
(NEXTRON waits that out), and it may legitimately reuse a circuit that has not
been used yet. The activity log says so explicitly. If it never changes, check
that `verify_exit_after_rotation` is on and that `rotation_retries` is not 0.

### "Cannot authenticate to the Tor ControlPort"

You are attaching to a system daemon whose cookie file is not readable by your
user:

```bash
sudo usermod -aG debian-tor "$USER"   # then log out and back in
```

Or let NEXTRON manage its own daemon (`manage_daemon = true`), which uses a
cookie in its own data directory.

---

## VPN

### The tunnel never comes up

Read the tail of the OpenVPN log:

```bash
tail -50 ~/.config/nextron/logs/openvpn.log
```

| Log line | Cause |
|---|---|
| `AUTH_FAILED` | Wrong credentials, or the profile needs an auth file — see below |
| `TLS handshake failed` | Wrong port/protocol, or the endpoint is unreachable |
| `Cannot resolve host address` | DNS is broken — if the Shield is on, check its upstream |
| `Operation not permitted` | Not running as root |

### A profile needs a username and password

NEXTRON never asks for or stores VPN passwords. Create the file yourself:

```bash
printf '%s\n%s\n' 'myuser' 'mypassword' > ~/.vpn-creds
chmod 600 ~/.vpn-creds
```

Then in the VPN Library (`V`) press `U` and give it that path. NEXTRON records
the path only; it never reads or copies the contents.

### "WireGuard is UDP-only and cannot traverse Tor's SOCKS proxy"

Correct, and not a bug. *VPN over Tor* needs a TCP OpenVPN profile. Either use a
profile with `proto tcp`, or choose *Tor over VPN*, where WireGuard works fine.

### The network is dead after NEXTRON exited badly

The kill switch was left armed. NEXTRON removes stale rules at the next launch,
and you can also remove them by hand:

```bash
sudo nft delete table inet nextron_killswitch
sudo nft delete table ip nextron_transparent
```

With iptables instead of nftables:

```bash
sudo iptables -S OUTPUT | grep NEXTRON-KILLSWITCH
# delete each matching rule with: sudo iptables -D OUTPUT <rule…>
```

### The kill switch says "off"

The status line names the reason: *disabled in settings*, *root privileges
unavailable*, *neither nft nor iptables is installed*, or — in *VPN over Tor* —
deliberately off, because Tor needs direct egress to reach guard relays.

---

## DNS Shield

### It fell back to port 5353

Port 53 needs root. Either run NEXTRON with `sudo`, or point individual
applications at `127.0.0.1:5353`. Nothing else on the system is protected in
that case, and the activity log says so.

### A site is broken and I think the Shield is blocking it

```bash
nextron dns check thesite.example
```

If it reports BLOCKED, whitelist it — the whitelist beats every list, including
explicit subdomain entries:

```bash
nextron dns whitelist thesite.example
```

Or press `W` on the DNS Shield screen.

### DNS stopped working entirely after NEXTRON exited badly

`/etc/resolv.conf` was left pointing at a Shield that is no longer running. The
original is saved:

```bash
sudo cp ~/.config/nextron/runtime/resolv.conf.nextron-backup /etc/resolv.conf
```

### Blocked domain count is 0

No list has been downloaded yet. The catalogue ships with NEXTRON, but the lists
themselves are fetched on request:

```bash
nextron dns update
```

or press `U` on the DNS Shield screen. If that still leaves you at zero, check
`nextron dns sources` — a catalogue with nothing enabled blocks nothing.

### Some lists "failed" to download

Three outcomes are reported separately, and only one is a problem:

| Reported | Meaning |
|---|---|
| downloaded | converted and in use |
| cosmetic-only | the list contains only element-hiding rules; DNS cannot enforce them. Not an error. |
| unreachable | HTTP 404/403, connection refused, or a timeout |

Unreachable sources are usually lists that moved or were withdrawn. They stay in
the catalogue so you can see what was tried; comment them out (or leave them —
they are retried on each update).

### A list I want is in the catalogue but disabled

Entries shipped commented out were checked and found unusable — the reason is on
the line. Remove the leading `#` in `~/.config/nextron/dns/sources.txt` to try
again, then run `nextron dns update`.

### systemd-resolved conflicts

NEXTRON binds `127.0.0.1:53`, while systemd-resolved uses `127.0.0.53:53`, so the
two do not collide. If your `resolv.conf` is a symlink managed by resolved, the
redirect may be reverted by the system; in that case either disable resolved's
stub or point applications at the Shield directly.

---

## Verification

### "Verification failed" and the mode was torn down

That is `verification.strict` doing its job: rather than leaving you connected to
something you did not ask for, the route is removed. Press `I` for the full
report — the failing check names the reason. To connect anyway, turn strict mode
off in Settings, accepting that a "Connected" label then means less.

### DNS leak warning while the VPN is up

Your system is querying a public resolver outside the tunnel. Enable the DNS
Shield (`D`); with Tor running it will also route queries through Tor's
`DNSPort`.

### IPv6 leak warning

Your tunnel carries IPv4 only while IPv6 reaches the internet directly. Either
use a profile that carries IPv6, disable IPv6 on the interface, or set
`dns.block_ipv6_answers = true` so names never resolve to IPv6 in the first
place.

---

## Importing files

### Pressing `I` does not open a file browser

NEXTRON asks the desktop for its chooser; one must be installed and reachable.

```bash
sudo apt install zenity      # or kdialog, yad, qarma
echo "$DISPLAY $WAYLAND_DISPLAY"
```

If neither variable is set you are on a plain TTY or an SSH session without
forwarding — press `O` instead and type the path. `NEXTRON_NO_FILE_DIALOG=1`
forces that behaviour permanently.

### "The file chooser could not reach the desktop session"

You are running NEXTRON under `sudo`, which usually loses access to your X or
Wayland session. Either import first without `sudo` (the library lives in your
own configuration directory), or press `O` and type the path.

### Only some of my selected files imported

Each file is imported independently and failures are reported by name, so one
unsupported file cannot stop the rest. The usual causes are an unsupported
extension and a WireGuard file with no `[Interface]` section.
`nextron vpn import <file>` prints the specific reason.

## Imported profiles

### A profile fails with a TLS handshake error

Check the **Cert** column in the VPN Library (`V`). Many published profiles
embed a client certificate with a fixed lifetime; once it reads `EXPIRED` the
server will refuse the connection. Download a current profile from the provider
and import it with `I`, or drop it into `Sources/VPN Profiles`. `nextron doctor`
warns 30 days ahead.

### A profile I dropped into Sources did not appear

Check the extension (`.ovpn`, `.conf`, `.wgconf`, `.json`) and that the file has
a server address — a config with no `remote` line (or no `Endpoint =` for
WireGuard) is refused, with the reason in the activity log. A copy of a profile
already in the library is skipped even if you renamed the file, because it is
matched by its contents.

### Should I trust a free public VPN server?

Free, publicly listed relays are fine for trying the VPN modes, not a basis for
anything sensitive. You do not control them and they can log what they carry.
Import a profile from a provider you have chosen yourself for real use.

## Interface

### The banner shows as ASCII art

The PNG path needs Pillow, the asset file and a truecolor-capable terminal.

```bash
echo "$COLORTERM"        # expect truecolor or 24bit
python -c "import PIL; print(PIL.__version__)"
nextron banner           # prints which path was used
```

`NEXTRON_FORCE_ASCII=1` forces the fallback deliberately.

### The layout is cramped

The dashboard wants roughly 120×40. Below that, columns compress and the panels
scroll — every panel is still reachable with `←` `→` and `↑` `↓`.

### Colours look wrong

NEXTRON uses only its five brand colours plus darkened derivations of two of
them. If they render oddly, the terminal is probably not in truecolor mode or a
terminal theme is overriding the palette.

---

## Logs

```bash
tail -f ~/.config/nextron/logs/nextron.log
```

Or press `L` in the interface, with `F` to filter by level. For maximum detail,
set `log_level = "DEBUG"` in Settings — every subprocess invocation, Tor notice
line and DNS decision is recorded.

---

### The selection highlight is hard to see

The highlight is deliberately dark — the brand primary `#4B0082` when focused,
a darker `#301F4C` when not — with near-white text on top. If it looks wrong,
the terminal is probably not in truecolor mode (`echo $COLORTERM` should print
`truecolor`) or a terminal theme is overriding the palette.

## Still stuck?

Attach the output of these two commands to a bug report:

```bash
nextron doctor --json > doctor.json
tail -200 ~/.config/nextron/logs/nextron.log > session.log
```

Both are local files. Review them before sharing — the log contains IP addresses
and VPN profile names.
