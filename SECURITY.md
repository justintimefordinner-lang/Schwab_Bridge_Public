# Security Policy

This is a personal, self-hosted project. By design it is **read-only** toward
your brokerage and keeps every secret on your own machine — see the "Security
notes" in the README. The app can only *write* into the bridge; it never reads
the bridge's credential files back.

## Reporting a vulnerability

If you find a security issue — especially anything that could expose
credentials, the OAuth token, or brokerage data — please report it **privately**
rather than opening a public issue.

- Preferred: this repository's **Security** tab → **Report a vulnerability**
  (GitHub private vulnerability reporting).

Please include clear steps to reproduce and the affected file(s). I'll
acknowledge and address confirmed issues as time allows.

This is a hobby project provided **as is, without warranty** (see
[LICENSE](LICENSE.md)). Nothing here is financial advice.
