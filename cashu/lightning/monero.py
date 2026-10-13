"""Monero wallet RPC backend for Cashu."""

import asyncio
import hashlib
import json
import os
import secrets
from decimal import Decimal
from typing import AsyncGenerator, Optional

import httpx
from loguru import logger

from ..core.base import Amount, MeltQuote, Unit
from ..core.models import PostMeltQuoteRequest
from .base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
)


class MoneroRPCError(Exception):
    """Monero RPC error with optional error code."""
    def __init__(self, message: str, code: Optional[int] = None):
        self.code = code
        super().__init__(message)


class MoneroBackend(LightningBackend):
    """Monero wallet RPC backend for Cashu mint."""

    supports_mpp = False
    supports_incoming_payment_stream = True
    supports_description = False
    supported_units = {Unit.pxmr, Unit.xmr}

    def __init__(
        self,
        unit: Unit = Unit.pxmr,
        rpc_url: str = "http://127.0.0.1:18082/json_rpc",
        rpc_username: Optional[str] = None,
        rpc_password: Optional[str] = None,
        min_confirmations: int = 10,
        state_file: Optional[str] = None,
        fee_reserve_fallback: int = 100_000_000,
        **kwargs
    ):
        self.unit = unit
        self.rpc_url = rpc_url
        self.rpc_username = rpc_username
        self.rpc_password = rpc_password
        # A deposit is only credited once its transaction has at least this
        # many confirmations. 10 is Monero's default spend-unlock window.
        self.min_confirmations = min_confirmations
        # Reserve used only when a live fee estimate can't be built.
        self.fee_reserve_fallback = fee_reserve_fallback
        # Deposit-tracking state is persisted here so a mint restart doesn't
        # strand in-flight deposits (which would lock users out of minting
        # tokens they've already paid for). None = in-memory only.
        self.state_file = state_file

        self.pending_deposits, self.pending_payments = self._load_state()
        # Queue for incoming payment notifications
        self.paid_invoices_queue: asyncio.Queue[str] = asyncio.Queue()

        # Polling interval for checking incoming payments (seconds)
        self.poll_interval = 5
        self._polling_task: Optional[asyncio.Task] = None

    def _load_state(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """Load deposit and outgoing-payment state, including legacy files."""
        if not self.state_file or not os.path.exists(self.state_file):
            return {}, {}
        try:
            with open(self.state_file) as f:
                state = json.load(f)
                if "pending_deposits" in state or "pending_payments" in state:
                    return (
                        state.get("pending_deposits", {}),
                        state.get("pending_payments", {}),
                    )
                # Before outgoing payments were persisted, this file was the
                # pending-deposit mapping itself. Keep those files readable.
                return state, {}
        except (OSError, ValueError):
            logger.error(f"Could not read Monero state from {self.state_file}")
            return {}, {}

    def _save_state(self) -> bool:
        """Persist payment state via an atomic replace."""
        if not self.state_file:
            return True
        try:
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w") as f:
                json.dump(
                    {
                        "pending_deposits": self.pending_deposits,
                        "pending_payments": self.pending_payments,
                    },
                    f,
                )
            os.replace(tmp, self.state_file)
            return True
        except OSError as e:
            logger.error(f"Could not persist Monero state: {e}")
            return False

    @staticmethod
    def validate_monero_address(address: str) -> tuple[bool, str]:
        """Validate Monero address format. Returns (is_valid, error_message)."""
        if not address:
            return False, "Address is empty"

        # mainnet: 4/8, testnet: 9/A, stagenet: 5/7
        if address[0] not in ('4', '8', '9', 'A', '5', '7'):
            return False, f"Invalid address prefix '{address[0]}'"

        if len(address) not in (95, 106):
            return False, f"Invalid address length: {len(address)}"

        base58_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        if not all(c in base58_chars for c in address):
            return False, "Invalid characters in address"

        return True, ""

    async def validate_address_rpc(self, address: str) -> bool:
        """Authoritatively validate an address via the wallet's own validator.

        The static check only covers prefix/length/charset; it cannot verify
        Monero's base58 keccak checksum, so a typo'd address can pass it. Before
        sending funds we ask the wallet, which checks the checksum and network.
        Fails closed on any RPC error.
        """
        try:
            result = await self._rpc_call("validate_address", {
                "address": address,
                "any_net_type": False,
                "allow_openalias": False,
            })
            return bool(result.get("valid"))
        except MoneroRPCError:
            return False

    def _parse_monero_uri(self, uri: str) -> tuple[str, int, Optional[str]]:
        """Parse monero: URI or plain address. Returns (address, amount_pico, description)."""
        address = uri
        amount = 0
        description = None

        if uri.startswith("monero:"):
            parts = uri.replace("monero:", "").split("?")
            address = parts[0]

            if len(parts) > 1:
                params = dict(p.split("=", 1) for p in parts[1].split("&") if "=" in p)
                if "tx_amount" in params:
                    amount = int(Decimal(params["tx_amount"]) * 1_000_000_000_000)
                description = params.get("tx_description")

        is_valid, error = self.validate_monero_address(address)
        if not is_valid:
            raise MoneroRPCError(f"Invalid address: {error}", code=-7)

        return address, amount, description

    async def _rpc_call(self, method: str, params: Optional[dict] = None, retries: int = 3) -> dict:
        """Make JSON-RPC call to Monero wallet with retries."""
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
        auth = httpx.DigestAuth(self.rpc_username, self.rpc_password) if self.rpc_username and self.rpc_password else None

        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    kwargs: dict = {"url": self.rpc_url, "json": payload, "timeout": 30.0}
                    if auth:
                        kwargs["auth"] = auth
                    response = await client.post(**kwargs)
                    response.raise_for_status()
                    result = response.json()

                    if "error" in result:
                        err = result["error"]
                        code = err.get("code") if isinstance(err, dict) else None
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        raise MoneroRPCError(msg, code=code)

                    return result.get("result", {})

            except httpx.ConnectError as e:
                last_error = MoneroRPCError(f"Connection failed: {e}")
            except httpx.TimeoutException as e:
                last_error = MoneroRPCError(f"Timeout: {e}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    raise MoneroRPCError("Auth failed", code=e.response.status_code)
                last_error = MoneroRPCError(f"HTTP {e.response.status_code}")
            except MoneroRPCError:
                raise
            except Exception as e:
                last_error = MoneroRPCError(str(e))

            if attempt < retries - 1:
                await asyncio.sleep(1.0 * (attempt + 1))

        raise last_error or MoneroRPCError("RPC failed")

    async def status(self) -> StatusResponse:
        """Get wallet balance."""
        try:
            result = await self._rpc_call("get_balance")
            unlocked = result.get("unlocked_balance", 0)
            return StatusResponse(balance=Amount(self.unit, unlocked), error_message=None)
        except MoneroRPCError as e:
            logger.error(f"Monero status error: {e}")
            return StatusResponse(balance=Amount(self.unit, 0), error_message=str(e))

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
    ) -> InvoiceResponse:
        """Create a subaddress for deposit."""
        try:
            result = await self._rpc_call("create_address", {"account_index": 0, "label": memo or "cashu"})
            address = result.get("address")
            if not address:
                return InvoiceResponse(ok=False, error_message="Failed to create subaddress")

            checking_id = hashlib.sha256(address.encode()).hexdigest()
            self.pending_deposits[checking_id] = {
                "address": address,
                "address_index": result.get("address_index"),
                "amount": amount.amount,
                "paid": False
            }
            self._save_state()

            amount_xmr = Decimal(amount.amount) / Decimal(10**12)
            payment_request = f"monero:{address}?tx_amount={amount_xmr:.12f}"

            return InvoiceResponse(ok=True, checking_id=checking_id, payment_request=payment_request)
        except MoneroRPCError as e:
            return InvoiceResponse(ok=False, error_message=str(e))

    async def pay_invoice(self, quote: MeltQuote, fee_limit_msat: int) -> PaymentResponse:
        """Send XMR to address in quote.request."""
        try:
            address, parsed_amount, _ = self._parse_monero_uri(quote.request)
        except MoneroRPCError as e:
            return PaymentResponse(result=PaymentResult.FAILED, error_message=str(e))

        amount = quote.amount if quote.amount > 0 else parsed_amount
        if amount <= 0:
            return PaymentResponse(result=PaymentResult.FAILED, error_message="Amount must be positive")

        # Last line of defence before funds leave: the static parse only checked
        # format. Verify the checksum/network with the wallet itself — sending to
        # an address that merely "looks" valid would lose the XMR for good.
        if not await self.validate_address_rpc(address):
            return PaymentResponse(result=PaymentResult.FAILED, error_message="Invalid Monero address")

        try:
            # Build first and relay separately. Retrying a relaying `transfer`
            # after a timeout can pay the recipient twice because the request
            # is not idempotent. A do-not-relay build is safe to abandon, and
            # retrying `relay_tx` reuses the exact same signed transaction.
            result = await self._rpc_call(
                "transfer",
                {
                    "destinations": [{"amount": amount, "address": address}],
                    "priority": 1,
                    "do_not_relay": True,
                    "get_tx_key": True,
                    "get_tx_metadata": True,
                },
                retries=1,
            )

            tx_hash = result.get("tx_hash")
            tx_metadata = result.get("tx_metadata")
            if not tx_hash or not tx_metadata:
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message="Transaction build returned incomplete metadata",
                )

            fee = int(result.get("fee", 0))
            if fee > quote.fee_reserve:
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message="Monero fee exceeds the quoted reserve",
                )

            # Keyed by the quote, not the txid, so the ledger can still find
            # it after a restart that came before it stored the txid.
            self.pending_payments[quote.checking_id] = {
                "tx_hash": tx_hash,
                "tx_metadata": tx_metadata,
                "amount": amount,
                "fee": fee,
            }
            # Never relay a transaction whose recovery metadata could not be
            # saved: a process crash after an ambiguous relay would otherwise
            # leave the Cashu melt unable to determine whether XMR was sent.
            if not self._save_state():
                self.pending_payments.pop(quote.checking_id, None)
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message="Could not persist outgoing payment state",
                )

            try:
                await self._rpc_call("relay_tx", {"hex": tx_metadata}, retries=1)
            except MoneroRPCError as e:
                # The relay may have reached wallet-rpc before the response was
                # lost. Keep the proofs pending and let the status check decide;
                # never construct a second payment.
                return PaymentResponse(
                    result=PaymentResult.PENDING,
                    checking_id=quote.checking_id,
                    fee=Amount(self.unit, fee),
                    error_message=str(e),
                )

            # No metadata left means the tx was relayed, so get_payment_status
            # will never relay it again.
            del self.pending_payments[quote.checking_id]["tx_metadata"]
            self._save_state()
            return PaymentResponse(
                result=PaymentResult.SETTLED,
                checking_id=quote.checking_id,
                fee=Amount(self.unit, fee),
                preimage=tx_hash,
            )
        except MoneroRPCError as e:
            # check if it's insufficient funds
            if e.code in (-17, -20):
                return PaymentResponse(result=PaymentResult.FAILED, error_message="Insufficient funds")
            return PaymentResponse(result=PaymentResult.FAILED, error_message=str(e))

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check if deposit was received."""
        if checking_id not in self.pending_deposits:
            return PaymentStatus(result=PaymentResult.UNKNOWN)

        deposit = self.pending_deposits[checking_id]
        if deposit.get("paid"):
            return PaymentStatus(result=PaymentResult.SETTLED, preimage=deposit.get("tx_hash"))

        try:
            # Only confirmed incoming transfers count. A transfer still in the
            # txpool has zero confirmations and can be dropped before it mines,
            # so crediting it would let a depositor mint tokens against funds
            # the mint never actually receives.
            result = await self._rpc_call("get_transfers", {
                "in": True,
                "subaddr_indices": [deposit["address_index"]],
            })

            for tx in result.get("in", []):
                # A confirmed output can still be locked by the transaction's
                # unlock_time. Do not issue liquid ecash until the wallet says
                # the backing output is spendable. Treat a missing `locked`
                # field as locked so an incompatible RPC response fails safe.
                if tx.get("locked", True):
                    continue
                if tx.get("confirmations", 0) < self.min_confirmations:
                    continue
                if tx.get("amount", 0) >= deposit["amount"]:
                    deposit["paid"] = True
                    deposit["tx_hash"] = tx.get("txid")
                    self._save_state()
                    try:
                        self.paid_invoices_queue.put_nowait(checking_id)
                    except asyncio.QueueFull:
                        pass
                    return PaymentStatus(result=PaymentResult.SETTLED, preimage=tx.get("txid"))

            return PaymentStatus(result=PaymentResult.PENDING)
        except MoneroRPCError as e:
            return PaymentStatus(result=PaymentResult.UNKNOWN, error_message=str(e))

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        """Check outgoing payment status."""
        payment = self.pending_payments.get(checking_id)
        # Older melts used the txid as their checking_id.
        txid = payment["tx_hash"] if payment else checking_id
        try:
            result = await self._rpc_call("get_transfer_by_txid", {"txid": txid})
        except MoneroRPCError as e:
            if not payment:
                return PaymentStatus(result=PaymentResult.UNKNOWN, error_message=str(e))
            if e.code != -8 or "tx_metadata" not in payment:
                return PaymentStatus(result=PaymentResult.PENDING, error_message=str(e))
            # -8 means the wallet has never seen this tx. relay_tx on a tx that
            # is already mined still succeeds, re-adds it as unconfirmed, and
            # wallet2 later marks its spent inputs unspent.
            try:
                await self._rpc_call("relay_tx", {"hex": payment["tx_metadata"]}, retries=1)
            except MoneroRPCError as e:
                return PaymentStatus(result=PaymentResult.PENDING, error_message=str(e))
            del payment["tx_metadata"]
            self._save_state()
            return PaymentStatus(
                result=PaymentResult.SETTLED,
                fee=Amount(self.unit, payment["fee"]),
                preimage=txid,
            )

        transfer = result.get("transfer", {})
        if transfer.get("type") == "failed":
            return PaymentStatus(result=PaymentResult.FAILED)
        if payment and payment.pop("tx_metadata", None):
            self._save_state()
        return PaymentStatus(
            result=PaymentResult.SETTLED,
            fee=Amount(self.unit, int(transfer.get("fee", 0))),
            preimage=txid,
        )

    async def _estimate_fee(self, address: str, amount: int) -> int:
        """Estimate the fee reserve for a withdrawal.

        Builds the tx without relaying it to read the real network fee — a flat
        hardcoded reserve loses the mint money whenever the true fee is higher
        (congestion, many inputs) — then adds 50% headroom since the rate can
        rise between quote and melt. Unused reserve is returned as change.
        Falls back to fee_reserve_fallback if the estimate can't be built.
        """
        if amount > 0:
            try:
                result = await self._rpc_call("transfer", {
                    "destinations": [{"amount": amount, "address": address}],
                    "priority": 1,
                    "do_not_relay": True,
                    "get_tx_metadata": True,
                }, retries=1)
                fee = int(result.get("fee", 0))
                if fee > 0:
                    return fee + fee // 2  # +50% headroom
            except MoneroRPCError:
                pass
        return self.fee_reserve_fallback

    async def get_payment_quote(self, melt_quote: PostMeltQuoteRequest) -> PaymentQuoteResponse:
        """Get quote for payment (with a live fee estimate)."""
        address, amount, _ = self._parse_monero_uri(melt_quote.request)
        fee = await self._estimate_fee(address, amount)

        # Unlike a bolt11 invoice, the same address and amount can rightly be
        # paid more than once, so the id cannot be derived from the request.
        checking_id = secrets.token_hex(32)
        return PaymentQuoteResponse(
            checking_id=checking_id,
            amount=Amount(self.unit, amount),
            fee=Amount(self.unit, fee),
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        """Stream paid invoice checking_ids."""
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._poll_incoming_payments())

        while True:
            yield await self.paid_invoices_queue.get()

    async def _poll_incoming_payments(self):
        """Poll for incoming payments."""
        while True:
            for checking_id, deposit in list(self.pending_deposits.items()):
                if not deposit.get("paid"):
                    try:
                        await self.get_invoice_status(checking_id)
                    except Exception:
                        pass
            await asyncio.sleep(self.poll_interval)
