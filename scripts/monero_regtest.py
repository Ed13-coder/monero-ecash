#!/usr/bin/env python
"""
Run the full Monero ecash cycle against a private regtest chain.

monerod, the wallet-rpc processes and the mint all run as real processes. A
payer wallet funds a mint quote, the deposit is only credited once it is ten
blocks deep, the ecash is minted, sent, received and melted, and the XMR has to
arrive in a destination wallet the mint holds no key for.

Usage:
    python scripts/monero_regtest.py --fetch ~/monero
    python scripts/monero_regtest.py \
        --bin-dir ~/monero/monero-x86_64-linux-gnu-v0.18.5.3

With --locked-bin-dir pointing at a v0.18.3.3 release it also sends a deposit
with a far-future unlock_time and checks the mint refuses to credit it. Newer
nodes no longer relay such transactions, but blocks containing them are still
valid, so the old release is only used for this last step.
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Callable, Optional

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cashu.core.base import MeltQuoteState, Method, MintQuoteState  # noqa: E402
from cashu.wallet.helpers import deserialize_token_from_string, receive  # noqa: E402
from cashu.wallet.wallet import Wallet  # noqa: E402

RELEASES = {
    "v0.18.5.3": "3333d0e04c9cbe3023e7e2b6dd369c407f36bb8df5b05fb0a19d7685b3d56dfc",
    "v0.18.3.3": "47c7e6b4b88a57205800a2538065a7874174cd087eedc2526bee1ebcce0cc5e3",
}
PICO = 10**12
DEPOSIT = PICO
SEND = 6 * PICO // 10
WITHDRAW = PICO // 2
LOCKED_DEPOSIT = 7 * PICO // 10
CONFIRMATIONS = 10


def fetch(dest: str, version: str) -> str:
    os.makedirs(dest, exist_ok=True)
    tarball = os.path.join(dest, f"monero-linux-x64-{version}.tar.bz2")
    if not os.path.exists(tarball):
        url = f"https://downloads.getmonero.org/cli/monero-linux-x64-{version}.tar.bz2"
        print(f"downloading {url}")
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(f"{tarball}.part", "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        os.replace(f"{tarball}.part", tarball)
    with open(tarball, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != RELEASES[version]:
        raise SystemExit(f"sha256 mismatch for {tarball}: {digest}")
    with tarfile.open(tarball) as tar:
        top = tar.getnames()[0].split("/")[0]
        tar.extractall(dest)
    return os.path.join(dest, top)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RPCError(Exception):
    pass


def wait_for(check: Callable[[], bool], what: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, RPCError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {what}")


class RPC:
    def __init__(self, port: int):
        self.url = f"http://127.0.0.1:{port}/json_rpc"

    def __call__(self, method: str, **params) -> dict:
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params}
        result = httpx.post(self.url, json=payload, timeout=120).json()
        if "error" in result:
            raise RPCError(f"{method}: {result['error']}")
        return result["result"]


class Chain:
    def __init__(self, work: str):
        self.work = work
        self.procs: list = []
        self.daemon_port = free_port()
        self.daemon: Optional[subprocess.Popen] = None

    def spawn(self, name: str, args: list) -> subprocess.Popen:
        log = open(os.path.join(self.work, f"{name}.out"), "w")
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc

    def start_daemon(self, bin_dir: str, keep: bool = False) -> None:
        data = os.path.join(self.work, "daemon")
        args = [
            os.path.join(bin_dir, "monerod"), "--regtest", "--offline",
            "--fixed-difficulty", "1", "--no-igd", "--hide-my-port",
            "--data-dir", data, "--log-file", os.path.join(data, "monerod.log"),
            "--rpc-bind-ip", "127.0.0.1", "--rpc-bind-port", str(self.daemon_port),
            "--p2p-bind-ip", "127.0.0.1", "--p2p-bind-port", str(free_port()),
            "--zmq-rpc-bind-port", str(free_port()), "--non-interactive",
            "--disable-dns-checkpoints", "--rpc-ssl", "disabled",
        ]
        # monerod wipes a fakechain on start unless told otherwise.
        if keep:
            args.append("--keep-fakechain")
        self.daemon = self.spawn("monerod", args)
        rpc = RPC(self.daemon_port)
        wait_for(lambda: rpc("get_info")["status"] == "OK", "monerod")

    def stop_daemon(self) -> None:
        assert self.daemon
        self.daemon.terminate()
        self.daemon.wait(timeout=60)

    def start_wallet_rpc(self, name: str, bin_dir: str) -> RPC:
        port = free_port()
        wallet_dir = os.path.join(self.work, name)
        os.makedirs(wallet_dir, exist_ok=True)
        self.spawn(name, [
            os.path.join(bin_dir, "monero-wallet-rpc"), "--wallet-dir", wallet_dir,
            "--disable-rpc-login",
            "--daemon-address", f"127.0.0.1:{self.daemon_port}",
            "--trusted-daemon", "--rpc-bind-ip", "127.0.0.1",
            "--rpc-bind-port", str(port), "--non-interactive",
            "--log-file", os.path.join(wallet_dir, "wallet-rpc.log"),
            "--daemon-ssl", "disabled", "--rpc-ssl", "disabled",
            "--shared-ringdb-dir", os.path.join(wallet_dir, "ringdb"),
            # Since v0.18.1.1 the wallet checks block versions against the
            # mainnet fork table and refuses a regtest chain without this.
            "--allow-mismatched-daemon-version",
        ])
        rpc = RPC(port)
        wait_for(lambda: rpc("get_version") is not None, f"{name} wallet-rpc")
        return rpc

    def mine(self, address: str, blocks: int) -> None:
        RPC(self.daemon_port)(
            "generateblocks",
            wallet_address=address,
            amount_of_blocks=blocks,
            starting_nonce=0,
        )

    def height(self) -> int:
        return RPC(self.daemon_port)("get_info")["height"]

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()


def synced(chain: Chain, rpc: RPC) -> None:
    target = chain.height()
    wait_for(lambda: rpc("get_height")["height"] >= target, "wallet sync")


async def mint_quote_state(wallet: Wallet, quote_id: str) -> MintQuoteState:
    quote = await wallet.get_mint_quote(quote_id, method=Method.monero)
    return quote.state


async def run(args: argparse.Namespace) -> dict:
    work = tempfile.mkdtemp(prefix="monero-regtest-")
    chain = Chain(work)
    report: dict = {"monero": os.path.basename(args.bin_dir.rstrip("/"))}
    mint_proc = None
    try:
        chain.start_daemon(args.bin_dir)
        mint_rpc = chain.start_wallet_rpc("mint-wallet", args.bin_dir)
        user_rpc = chain.start_wallet_rpc("user-wallet", args.bin_dir)

        mint_rpc("create_wallet", filename="mint", language="English")
        # The default 20 s auto refresh only slows the run down; the mint
        # itself never calls refresh.
        mint_rpc("auto_refresh", enable=True, period=1)
        user_rpc("create_wallet", filename="destination", language="English")
        destination = user_rpc("get_address")["address"]
        user_rpc("create_wallet", filename="payer", language="English")
        payer = user_rpc("get_address")["address"]

        # Coinbase outputs unlock after 60 blocks, so mine past that to leave
        # the payer spendable outputs.
        chain.mine(payer, 140)

        mint_port = free_port()
        env = dict(
            os.environ,
            PYTHONPATH=ROOT,
            CASHU_DIR=os.path.join(work, "cashu"),
            MINT_DATABASE=os.path.join(work, "mint-db"),
            MINT_PRIVATE_KEY=secrets.token_hex(32),
            MINT_LISTEN_PORT=str(mint_port),
            MINT_BACKEND_BOLT11_SAT="",
            MINT_BACKEND_MONERO_XMR="MoneroBackend",
            # Keysets take their unit from the derivation path, 6 is pxmr.
            MINT_DERIVATION_PATH_LIST='["m/0\'/6\'/0\'"]',
            MINT_MONERO_RPC_URL=mint_rpc.url,
            MINT_MONERO_MIN_CONFIRMATIONS=str(CONFIRMATIONS),
        )
        log = open(os.path.join(work, "mint.out"), "w")
        mint_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "cashu.mint.app:app",
             "--host", "127.0.0.1", "--port", str(mint_port)],
            cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        mint_url = f"http://127.0.0.1:{mint_port}"
        info = f"{mint_url}/v1/info"
        wait_for(lambda: httpx.get(info).status_code == 200, "mint", 120)

        alice = await Wallet.with_db(
            mint_url, os.path.join(work, "alice"), name="alice", unit="pxmr"
        )
        await alice.load_mint()
        bob = await Wallet.with_db(
            mint_url, os.path.join(work, "bob"), name="bob", unit="pxmr"
        )
        await bob.load_mint()

        quote = await alice.request_mint(DEPOSIT, method=Method.monero)
        address = quote.request.replace("monero:", "").split("?")[0]
        user_rpc("refresh")
        deposit = user_rpc(
            "transfer",
            destinations=[{"amount": DEPOSIT, "address": address}],
            priority=1,
        )
        report["deposit_txid"] = deposit["tx_hash"]
        print(f"deposit {deposit['tx_hash']} to {address}")

        states = []
        for confirmations in range(1, CONFIRMATIONS + 1):
            chain.mine(payer, 1)
            synced(chain, mint_rpc)
            state = await mint_quote_state(alice, quote.quote)
            states.append((confirmations, state.value))
            print(f"  {confirmations} confirmations: mint quote {state.value}")
            if confirmations < CONFIRMATIONS and state != MintQuoteState.unpaid:
                raise SystemExit(f"deposit credited at {confirmations} confirmations")
        if state != MintQuoteState.paid:
            raise SystemExit(f"deposit not credited at {CONFIRMATIONS} confirmations")
        report["deposit_states"] = states

        await alice.mint(DEPOSIT, quote_id=quote.quote, method=Method.monero)
        await alice.load_proofs(reload=True)
        minted = alice.available_balance.amount
        if minted != DEPOSIT:
            raise SystemExit(f"alice minted {minted}, expected {DEPOSIT}")
        print(f"alice minted {minted} pxmr")

        _, send_proofs = await alice.swap_to_send(alice.proofs, SEND, set_reserved=True)
        token = await alice.serialize_proofs(send_proofs)
        await receive(bob, deserialize_token_from_string(token))
        await bob.load_proofs(reload=True)
        received = bob.available_balance.amount
        # Receiving swaps the proofs, which costs the keyset input fee.
        if not SEND - 100 <= received <= SEND:
            raise SystemExit(f"bob received {received}, expected about {SEND}")
        report["bob_received"] = received
        print(f"bob received {received} pxmr")

        request = f"monero:{destination}?tx_amount={WITHDRAW / PICO:.12f}"
        melt_quote = await bob.melt_quote(request, method=Method.monero)
        proofs, _ = await bob.select_to_send(
            bob.proofs, melt_quote.amount + melt_quote.fee_reserve, include_fees=True
        )
        melted = await bob.melt(
            proofs,
            request,
            melt_quote.fee_reserve,
            melt_quote.quote,
            method=Method.monero,
        )
        print(f"melt {melt_quote.quote}: {melted.state}")
        if melted.state != MeltQuoteState.paid.value:
            raise SystemExit(f"melt not paid once relayed: {melted.state}")
        withdrawal_txid = melted.payment_preimage
        report["withdrawal_txid"] = withdrawal_txid
        fee = mint_rpc("get_transfer_by_txid", txid=withdrawal_txid)["transfer"]["fee"]
        if fee > melt_quote.fee_reserve:
            raise SystemExit(f"fee {fee} above reserve {melt_quote.fee_reserve}")
        report["fee_reserve"] = melt_quote.fee_reserve
        report["network_fee"] = fee
        await bob.load_proofs(reload=True)
        report["bob_after_melt"] = bob.available_balance.amount
        print(
            f"withdrawal {withdrawal_txid}, fee reserve {melt_quote.fee_reserve}, "
            f"network fee {fee}, bob left with {bob.available_balance.amount}"
        )

        chain.mine(payer, CONFIRMATIONS)
        user_rpc("close_wallet")
        user_rpc("open_wallet", filename="destination")
        user_rpc("refresh")
        incoming = user_rpc("get_transfers", **{"in": True}).get("in", [])
        arrived = [t for t in incoming if t["txid"] == withdrawal_txid]
        if not arrived or arrived[0]["amount"] != WITHDRAW or arrived[0]["locked"]:
            raise SystemExit(f"withdrawal not at the destination: {incoming}")
        report["destination_received"] = arrived[0]["amount"]
        report["destination_confirmations"] = arrived[0]["confirmations"]
        print(
            f"destination received {arrived[0]['amount']} pxmr, "
            f"{arrived[0]['confirmations']} confirmations"
        )

        if args.locked_bin_dir:
            locked = await locked_deposit(chain, args, alice, mint_rpc)
            report["locked_deposit"] = locked
        return report
    finally:
        if mint_proc and mint_proc.poll() is None:
            mint_proc.terminate()
            mint_proc.wait(timeout=30)
        chain.close()
        if args.keep:
            print(f"work dir kept at {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


async def locked_deposit(
    chain: Chain, args: argparse.Namespace, alice: Wallet, mint_rpc: RPC
) -> dict:
    chain.stop_daemon()
    chain.start_daemon(args.locked_bin_dir, keep=True)
    old_rpc = chain.start_wallet_rpc("old-wallet", args.locked_bin_dir)
    old_rpc("create_wallet", filename="locker", language="English")
    locker = old_rpc("get_address")["address"]
    chain.mine(locker, 70)
    old_rpc("refresh")

    quote = await alice.request_mint(LOCKED_DEPOSIT, method=Method.monero)
    address = quote.request.replace("monero:", "").split("?")[0]
    unlock_time = chain.height() + 100_000
    sent = old_rpc(
        "transfer",
        destinations=[{"amount": LOCKED_DEPOSIT, "address": address}],
        priority=1,
        unlock_time=unlock_time,
    )
    chain.mine(locker, 15)
    synced(chain, mint_rpc)
    incoming = mint_rpc("get_transfers", **{"in": True}).get("in", [])
    entry = next(t for t in incoming if t["txid"] == sent["tx_hash"])
    state = await mint_quote_state(alice, quote.quote)
    print(
        f"locked deposit {sent['tx_hash']}: {entry['confirmations']} confirmations, "
        f"locked {entry['locked']}, mint quote {state}"
    )
    if state != MintQuoteState.unpaid:
        raise SystemExit("time-locked deposit was credited")
    return {
        "txid": sent["tx_hash"],
        "unlock_time": unlock_time,
        "confirmations": entry["confirmations"],
        "locked": entry["locked"],
        "mint_quote_state": state.value,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bin-dir", help="monerod and monero-wallet-rpc directory")
    parser.add_argument("--fetch", metavar="DIR", help="download the pinned releases")
    parser.add_argument("--locked-bin-dir", help="v0.18.3.3 binaries, see above")
    parser.add_argument("--json", help="write the txids and states to this file")
    parser.add_argument("--keep", action="store_true", help="keep the work dir")
    args = parser.parse_args()

    if args.fetch:
        print(fetch(args.fetch, "v0.18.5.3"))
        print(fetch(args.fetch, "v0.18.3.3"))
        return
    if not args.bin_dir:
        parser.error("--bin-dir is required")

    report = asyncio.run(run(args))
    print(json.dumps(report, indent=2))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
