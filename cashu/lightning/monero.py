"""Monero wallet RPC backend for Cashu."""

import asyncio
import hashlib
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
        **kwargs
    ):
        self.unit = unit
        self.rpc_url = rpc_url
        self.rpc_username = rpc_username
        self.rpc_password = rpc_password

        self.pending_deposits: dict[str, dict] = {}
        self.pending_payments: dict[str, dict] = {}
        # Queue for incoming payment notifications
        self.paid_invoices_queue: asyncio.Queue[str] = asyncio.Queue()

        # Polling interval for checking incoming payments (seconds)
        self.poll_interval = 5
        self._polling_task: Optional[asyncio.Task] = None

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
                    amount = int(float(params["tx_amount"]) * 1e12)
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

            amount_xmr = amount.amount / 1e12
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

        try:
            result = await self._rpc_call("transfer", {
                "destinations": [{"amount": amount, "address": address}],
                "priority": 1,
                "get_tx_key": True
            })

            tx_hash = result.get("tx_hash")
            if not tx_hash:
                return PaymentResponse(result=PaymentResult.FAILED, error_message="No tx_hash returned")

            self.pending_payments[tx_hash] = {"tx_hash": tx_hash, "amount": amount, "fee": result.get("fee", 0)}

            return PaymentResponse(
                result=PaymentResult.SETTLED,
                checking_id=tx_hash,
                fee=Amount(self.unit, result.get("fee", 0)),
                preimage=tx_hash
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
            result = await self._rpc_call("get_transfers", {
                "in": True, "pending": True, "pool": True,
                "subaddr_indices": [deposit["address_index"]]
            })

            for ttype in ["in", "pending", "pool"]:
                for tx in result.get(ttype, []):
                    if tx.get("amount", 0) >= deposit["amount"]:
                        deposit["paid"] = True
                        deposit["tx_hash"] = tx.get("txid")
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
        if checking_id in self.pending_payments:
            return PaymentStatus(result=PaymentResult.SETTLED)

        try:
            result = await self._rpc_call("get_transfer_by_txid", {"txid": checking_id})
            if result.get("transfer", {}).get("confirmations", 0) > 0:
                return PaymentStatus(result=PaymentResult.SETTLED, preimage=checking_id)
            return PaymentStatus(result=PaymentResult.PENDING)
        except MoneroRPCError:
            return PaymentStatus(result=PaymentResult.UNKNOWN)

    async def get_payment_quote(self, melt_quote: PostMeltQuoteRequest) -> PaymentQuoteResponse:
        """Get quote for payment."""
        address, amount, _ = self._parse_monero_uri(melt_quote.request)
        fee = 100000000  # ~0.0001 XMR default

        checking_id = hashlib.sha256(melt_quote.request.encode()).hexdigest()
        return PaymentQuoteResponse(
            checking_id=checking_id,
            amount=Amount(self.unit, amount),
            fee=Amount(self.unit, fee)
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
