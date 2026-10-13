# Monero regtest run

This records one run of `scripts/monero_regtest.py` on 13 October 2026 against
the mint at 799026c. monerod, a wallet-rpc for the mint, one for the users and
the mint itself ran as separate processes on a private regtest chain, and the
client side was the cashu wallet from this repository.

The binaries were the official Linux x64 releases, checked against the sha256
values pinned in the script, which come from the GPG-signed hashes.txt of each
release. v0.18.5.3 ran the chain and the wallets until the last step; v0.18.3.3
was only used for the time-locked deposit at the end.

| Release   | sha256                                                             |
|-----------|--------------------------------------------------------------------|
| v0.18.5.3 | `3333d0e04c9cbe3023e7e2b6dd369c407f36bb8df5b05fb0a19d7685b3d56dfc` |
| v0.18.3.3 | `47c7e6b4b88a57205800a2538065a7874174cd087eedc2526bee1ebcce0cc5e3` |

Wallets from v0.18.1.1 on check block versions against the mainnet hard-fork
table and stop at height 1 of a regtest chain unless started with
`--allow-mismatched-daemon-version`. v0.18.1.0 is the last release that syncs
one without the flag. The script passes it, so there is no need to pin an old
wallet.

## What happened

A payer wallet sent 1 XMR to a fresh subaddress from a mint quote in
`5d9d59923790a365e0b114dc12b6decf5773fcb1e71644ec932462f81901f9ac`. The quote
was checked after every block. It read UNPAID at one through nine
confirmations and PAID at ten.

Alice minted 1 XMR of ecash against it and sent 0.6 XMR to Bob, who received
0.599999999998 XMR after the two piconero input fee on the swap. Bob melted
0.5 XMR to a destination wallet in the users' wallet-rpc, so the mint never
held a key for it. The melt came back PAID once the daemon had accepted
`3a7c9a1cd99f6a31241db95a1d133f0c41b7cfd82738b76d9f01a112111de85b`. Its fee was
0.0018084 XMR against a quoted reserve of 0.0027126 XMR, and the unused
reserve came back to Bob as change, leaving him 0.098191599996 XMR. Ten blocks
later the destination wallet held exactly 0.5 XMR from that transaction,
unlocked.

For the last step the chain moved to a v0.18.3.3 daemon, which still accepts
transactions with an `unlock_time` into its pool. A second quote was paid
0.7 XMR in `62bbc57bb8a82742188b3fef8682d745c733575e03a3b73ccff0a0f85541da91`
with `unlock_time` 100231. After fifteen blocks the mint wallet reported it
with 15 confirmations and `locked: true`, and the quote stayed UNPAID.

The same script run against main at 3c2babd got through the cycle and then
failed that last check. Its time-locked deposit,
`ad1f4611cf935ab5d44d36d5154ce2a8e3a66bf6b770c86cf975f8059c661741`, also had 15
confirmations and `locked: true`, and the quote went to PAID. Main would have
issued spendable ecash against XMR it could not spend for another 100000
blocks.

## Repeating it

These transactions are on a private chain and every rerun produces new ones,
so compare the states, amounts and fee rather than the ids.

```bash
python scripts/monero_regtest.py --fetch ~/monero
python scripts/monero_regtest.py --bin-dir ~/monero/monero-x86_64-linux-gnu-v0.18.5.3 \
  --locked-bin-dir ~/monero/monero-x86_64-linux-gnu-v0.18.3.3 --json run.json
```

The run takes about a minute after the download. CI runs it on pull requests
and on main and keeps the JSON as the `monero-regtest` artifact.

No stagenet run is recorded yet, so the stagenet item in MONERO_README.md stays
open.
