"""
Monero backend for Cashu ecash mint.

This module provides Monero wallet RPC integration for the Cashu protocol,
enabling ecash tokens backed by Monero instead of Bitcoin Lightning.
"""

import asyncio
import hashlib
import json
from typing import AsyncGenerator, Dict, Optional

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
    """Exception raised for Monero RPC errors."""
    pass


class MoneroBackend(LightningBackend):
    """
    Monero wallet RPC backend for Cashu mint.

    This backend interfaces with a Monero wallet via JSON-RPC to:
    - Create subaddresses for receiving deposits (mint)
    - Send Monero payments for redemptions (melt)
    - Track payment status
    """

    supports_mpp: bool = False
    supports_incoming_payment_stream: bool = True
    supports_description: bool = False
    supported_units = {Unit.pxmr, Unit.xmr}  # piconero (atomic) and XMR

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

        # Track created subaddresses: {checking_id: {address, amount, account_index, address_index}}
        self.pending_deposits: Dict[str, dict] = {}
        # Track outgoing payments: {checking_id: {tx_hash, status}}
        self.pending_payments: Dict[str, dict] = {}
        # Queue for incoming payment notifications
        self.paid_invoices_queue: asyncio.Queue[str] = asyncio.Queue()

        # Polling interval for checking incoming payments (seconds)
        self.poll_interval = 5
        self._polling_task: Optional[asyncio.Task] = None

    async def _rpc_call(self, method: str, params: Optional[dict] = None) -> dict:
        """Make a JSON-RPC call to the Monero wallet."""
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {}
        }

        auth = None
        if self.rpc_username and self.rpc_password:
            auth = httpx.DigestAuth(self.rpc_username, self.rpc_password)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.rpc_url,
                    json=payload,
                    auth=auth,
                    timeout=30.0
                )
                response.raise_for_status()
                result = response.json()

                if "error" in result:
                    raise MoneroRPCError(f"RPC error: {result['error']}")

                return result.get("result", {})
            except httpx.HTTPError as e:
                raise MoneroRPCError(f"HTTP error: {e}")

    async def status(self) -> StatusResponse:
        """Get wallet balance."""
        try:
            result = await self._rpc_call("get_balance")
            # Balance is in atomic units (piconero), convert based on unit
            balance_atomic = result.get("balance", 0)
            unlocked_balance = result.get("unlocked_balance", 0)

            # For now, use unlocked balance and treat as sat equivalent
            # 1 XMR = 1e12 piconero, 1 BTC = 1e8 sat
            # We'll use piconero directly as "sat" equivalent
            balance = Amount(self.unit, unlocked_balance)

            return StatusResponse(balance=balance, error_message=None)
        except MoneroRPCError as e:
            logger.error(f"Monero status error: {e}")
            return StatusResponse(
                balance=Amount(self.unit, 0),
                error_message=str(e)
            )

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
    ) -> InvoiceResponse:
        """
        Create a new subaddress for receiving a deposit.

        In Monero, we create a unique subaddress for each deposit request.
        The 'invoice' is the subaddress + amount encoded as a monero: URI.
        """
        try:
            # Create a new subaddress
            label = memo or "cashu_deposit"
            result = await self._rpc_call("create_address", {
                "account_index": 0,
                "label": label
            })

            address = result.get("address")
            address_index = result.get("address_index")

            if not address:
                return InvoiceResponse(
                    ok=False,
                    error_message="Failed to create subaddress"
                )

            # Generate a unique checking_id from the address
            checking_id = hashlib.sha256(address.encode()).hexdigest()

            # Store pending deposit info
            self.pending_deposits[checking_id] = {
                "address": address,
                "address_index": address_index,
                "amount": amount.amount,
                "paid": False
            }

            # Create Monero URI as payment_request
            # Format: monero:address?tx_amount=amount_in_xmr
            amount_xmr = amount.amount / 1e12  # Convert piconero to XMR
            payment_request = f"monero:{address}?tx_amount={amount_xmr:.12f}"
            if memo:
                payment_request += f"&tx_description={memo}"

            logger.info(f"Created Monero deposit address: {address[:16]}... for {amount_xmr} XMR")

            return InvoiceResponse(
                ok=True,
                checking_id=checking_id,
                payment_request=payment_request
            )
        except MoneroRPCError as e:
            logger.error(f"Failed to create invoice: {e}")
            return InvoiceResponse(
                ok=False,
                error_message=str(e)
            )

    async def pay_invoice(
        self,
        quote: MeltQuote,
        fee_limit_msat: int
    ) -> PaymentResponse:
        """
        Send a Monero payment.

        The quote.request should contain a Monero address or URI.
        """
        try:
            # Parse the payment request (could be address or monero: URI)
            request = quote.request
            address = request
            amount = quote.amount

            if request.startswith("monero:"):
                # Parse URI
                parts = request.replace("monero:", "").split("?")
                address = parts[0]
                if len(parts) > 1:
                    params = dict(p.split("=") for p in parts[1].split("&"))
                    if "tx_amount" in params:
                        amount = int(float(params["tx_amount"]) * 1e12)

            # Send the transfer
            result = await self._rpc_call("transfer", {
                "destinations": [{
                    "amount": amount,
                    "address": address
                }],
                "priority": 1,  # Normal priority
                "get_tx_key": True
            })

            tx_hash = result.get("tx_hash")
            fee = result.get("fee", 0)

            if not tx_hash:
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message="No transaction hash returned"
                )

            # Store payment info
            checking_id = tx_hash
            self.pending_payments[checking_id] = {
                "tx_hash": tx_hash,
                "status": "pending"
            }

            logger.info(f"Sent Monero payment: {tx_hash[:16]}...")

            return PaymentResponse(
                result=PaymentResult.SETTLED,  # Monero txs are instant once broadcast
                checking_id=checking_id,
                fee=Amount(self.unit, fee),
                preimage=tx_hash  # Use tx_hash as preimage equivalent
            )
        except MoneroRPCError as e:
            logger.error(f"Payment failed: {e}")
            return PaymentResponse(
                result=PaymentResult.FAILED,
                error_message=str(e)
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check if a deposit has been received."""
        if checking_id not in self.pending_deposits:
            return PaymentStatus(
                result=PaymentResult.UNKNOWN,
                error_message="Unknown invoice"
            )

        deposit = self.pending_deposits[checking_id]

        if deposit.get("paid"):
            return PaymentStatus(result=PaymentResult.SETTLED)

        try:
            # Check incoming transfers to this address
            result = await self._rpc_call("get_transfers", {
                "in": True,
                "pending": True,
                "pool": True,
                "subaddr_indices": [deposit["address_index"]]
            })

            # Check all transfer types
            for transfer_type in ["in", "pending", "pool"]:
                transfers = result.get(transfer_type, [])
                for tx in transfers:
                    if tx.get("amount", 0) >= deposit["amount"]:
                        # Payment received
                        deposit["paid"] = True
                        deposit["tx_hash"] = tx.get("txid")

                        # Notify via queue
                        await self.paid_invoices_queue.put(checking_id)

                        return PaymentStatus(
                            result=PaymentResult.SETTLED,
                            preimage=tx.get("txid")
                        )

            return PaymentStatus(result=PaymentResult.PENDING)
        except MoneroRPCError as e:
            logger.error(f"Failed to check invoice status: {e}")
            return PaymentStatus(
                result=PaymentResult.UNKNOWN,
                error_message=str(e)
            )

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        """Check outgoing payment status."""
        if checking_id not in self.pending_payments:
            # Check if it's a tx_hash we can look up
            try:
                result = await self._rpc_call("get_transfer_by_txid", {
                    "txid": checking_id
                })

                transfer = result.get("transfer", {})
                confirmations = transfer.get("confirmations", 0)

                if confirmations > 0:
                    return PaymentStatus(
                        result=PaymentResult.SETTLED,
                        preimage=checking_id
                    )
                else:
                    return PaymentStatus(result=PaymentResult.PENDING)
            except MoneroRPCError:
                return PaymentStatus(
                    result=PaymentResult.UNKNOWN,
                    error_message="Payment not found"
                )

        return PaymentStatus(result=PaymentResult.SETTLED)

    async def get_payment_quote(
        self,
        melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        """Get a quote for making a payment."""
        request = melt_quote.request

        # Parse amount from request
        amount = 0
        if request.startswith("monero:"):
            parts = request.replace("monero:", "").split("?")
            if len(parts) > 1:
                params = dict(p.split("=") for p in parts[1].split("&") if "=" in p)
                if "tx_amount" in params:
                    amount = int(float(params["tx_amount"]) * 1e12)

        # Estimate fee (rough estimate: ~0.0001 XMR for normal priority)
        estimated_fee = 100000000  # 0.0001 XMR in piconero

        # Generate checking_id from request
        checking_id = hashlib.sha256(request.encode()).hexdigest()

        return PaymentQuoteResponse(
            checking_id=checking_id,
            amount=Amount(self.unit, amount),
            fee=Amount(self.unit, estimated_fee)
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        """Stream of paid invoice checking_ids."""
        # Start background polling if not already running
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._poll_incoming_payments())

        while True:
            checking_id = await self.paid_invoices_queue.get()
            yield checking_id

    async def _poll_incoming_payments(self):
        """Background task to poll for incoming payments."""
        while True:
            try:
                for checking_id, deposit in list(self.pending_deposits.items()):
                    if not deposit.get("paid"):
                        await self.get_invoice_status(checking_id)
            except Exception as e:
                logger.error(f"Error polling payments: {e}")

            await asyncio.sleep(self.poll_interval)
