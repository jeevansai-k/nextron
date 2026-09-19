# NEXTRON — Routing modes

Exactly one mode is active at a time. Each one is built by a strict sequence and
is only reported as **Connected** after the verification engine agrees.

---

## 1. Tor Only

```text
You → Tor → Internet
```

**Sequence**

1. Write a private `torrc` to `~/.config/nextron/runtime/torrc` and launch the
   official `tor` binary (or attach to a daemon that is already running).
2. Follow the daemon's notice log until `Bootstrapped 100%`.
3. Connect Stem to the `ControlPort` using cookie authentication.
4. Engage transparent routing if possible, otherwise expose the SOCKS proxy.
5. Verify the exit with `check.torproject.org`.
6. Start the Tor rotation scheduler.

**Ports.** If something already holds Tor's preferred ports -- almost always the
distribution's own `tor.service` on 9050 -- NEXTRON moves its private daemon out
of the way (9050 → 9250) rather than failing, and says so in the activity log.
The port actually in use is shown on the dashboard, and that is the one to point
applications at.

**Reach.** Two possibilities, and the dashboard always says which one you have:

- *System-wide*: an nftables NAT ruleset redirects all outbound TCP to Tor's
  `TransPort` and all DNS to its `DNSPort`. Requires root **and** a Tor daemon
  running under a different user (see below).
- *SOCKS only*: Tor works, but only applications you point at
  `127.0.0.1:9050` use it.

**Why transparent routing needs a separate user.** The redirect must exempt the
Tor daemon's own traffic, or its connections to guard relays get redirected into
itself. The exemption is made on the daemon's uid (`meta skuid <uid> return`),
so the daemon cannot be running as you.

NEXTRON arranges this when it has root: it starts its own daemon under the
system Tor account (`debian-tor`), from `/var/lib/nextron`, and exempts that
uid. Nothing needs configuring:

```bash
sudo .venv/bin/nextron
```

**What else the ruleset closes.** A redirect alone is not enough. Tor's
`TransPort` carries IPv4 TCP, so while transparent routing is active NEXTRON
also drops outbound **IPv6** (a dual-stack host prefers it) and non-DNS
**UDP** (browsers speak QUIC on UDP 443). Both would otherwise leave the machine
untouched, carrying the real address. DHCP, loopback, link-local and the local
network stay permitted.

**Rotation.** The scheduler signals `NEWNYM`, waits out Tor's ten-second
coalescing window when necessary, then polls the exit address until it changes
(bounded by `tor.rotation_retries`). If the address does not change, that is
reported — Tor may legitimately have reused a clean circuit.

---

## 2. VPN Only

```text
You → VPN → Internet
```

**Sequence**

1. Arm the kill switch for the transition window, permitting only loopback,
   established connections, the target endpoint, the LAN and DHCP.
2. Connect the profile with `openvpn` or `wg-quick`.
   - OpenVPN: the process output is followed until
     `Initialization Sequence Completed`; the tunnel device and address are read
     from its log, not guessed.
   - WireGuard: `wg-quick up` is run and the interface is then confirmed to
     exist with an address.
3. Re-arm the kill switch with the live tunnel interface permitted, so a tunnel
   drop cannot leak to the default route.
4. Read the public address from behind the tunnel.
5. Verify it differs from the unprotected baseline captured at startup.
6. Start the VPN shuffle scheduler.

**Shuffle.** Independent of Tor. Four algorithms:

| Algorithm | Behaviour |
|---|---|
| Random | Uniform, never the profile already active |
| Sequential | Pool order, wrapping at the end |
| Round Robin | An internal queue: every profile once per cycle, regardless of what is active |
| No Repeat | A random permutation consumed to exhaustion, then reshuffled |

A failed switch falls back to the previous profile; if that also fails, the VPN
is reported as down instead of being silently left broken.

---

## 3. Tor over VPN

```text
You → VPN → Tor → Internet
```

The VPN hides Tor usage from your local network and ISP; Tor anonymises the
destination.

**Sequence**

1. Connect the VPN.
2. Verify the tunnel (interface, address, process or handshake age).
3. Launch Tor **after** the tunnel exists, so every relay connection it opens is
   created inside the VPN.
4. Engage transparent routing or expose SOCKS.
5. Verify with `check.torproject.org` **and** confirm the Tor exit address is not
   the VPN's address.

Both schedulers can run: Tor rotates identities while the VPN independently
shuffles profiles.

---

## 4. VPN over Tor

```text
You → Tor → VPN → Internet
```

Tor hides your VPN account from the provider; the exit is the VPN, not a relay.

**Sequence**

1. Launch Tor and wait for bootstrap.
2. Confirm the SOCKS proxy is accepting connections.
3. Generate a temporary runtime configuration from your profile — your original
   file is never modified.
4. Inject `socks-proxy 127.0.0.1 9050` and `socks-proxy-retry`.
5. Connect OpenVPN through Tor.
6. Verify the tunnel and confirm the VPN exit differs from both the Tor exit and
   the unprotected baseline.

**Requirements and honest limitations**

- A **TCP OpenVPN** profile is required. WireGuard is UDP-only and cannot
  traverse a SOCKS proxy; NEXTRON refuses with that explanation rather than
  timing out obscurely.
- The **kill switch stays off** in this mode. Tor needs direct egress to reach
  guard relays, so a default-drop egress filter would break the very path the
  tunnel rides on. NEXTRON logs this instead of implying protection.
- Expect latency. Every VPN packet crosses three Tor relays.

---

## DNS Shield is not a mode

It is an independent layer that works with all four modes and can be toggled at
any time with `D`. When Tor is running, the Shield uses Tor's `DNSPort` as its
upstream — that is what keeps DNS inside the tunnel. See
[DNS_SHIELD.md](DNS_SHIELD.md).

---

## Teardown

Teardown always happens in reverse order: transparent routing rules, then the
DNS Shield (restoring `/etc/resolv.conf`), then the VPN (releasing the kill
switch), then Tor. It runs on every exit path — an orderly quit, `Ctrl+C`, or an
unhandled error — and a session that crashed hard is cleaned up at the next
launch.
