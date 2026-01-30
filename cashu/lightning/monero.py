"""
Monero backend for Cashu ecash mint.

This module provides Monero wallet RPC integration for the Cashu protocol,
enabling ecash tokens backed by Monero instead of Bitcoin Lightning.
"""

import asyncio
import hashlib
from typing import AsyncGenerator, Dict, Optional, Tuple

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


class MoneroError(Exception):
    """Base exception for Monero-related errors."""
    pass


class MoneroRPCError(MoneroError):
    """Exception raised for Monero RPC errors."""
    def __init__(self, message: str, code: Optional[int] = None):
        self.code = code
        super().__init__(message)


class MoneroConnectionError(MoneroError):
    """Exception raised when unable to connect to Monero wallet RPC."""
    pass


class MoneroTimeoutError(MoneroError):
    """Exception raised when RPC call times out."""
    pass


class MoneroInsufficientFundsError(MoneroError):
    """Exception raised when wallet has insufficient funds."""
    pass


class MoneroInvalidAddressError(MoneroError):
    """Exception raised for invalid Monero addresses."""
    pass


class MoneroTransactionError(MoneroError):
    """Exception raised for transaction-related errors."""
    pass


# Monero RPC error codes
MONERO_RPC_ERRORS = {
    -1: "General RPC error",
    -2: "Invalid request",
    -3: "Method not found",
    -4: "Invalid params",
    -5: "Internal error",
    -6: "Unable to verify",
    -7: "Invalid address",
    -8: "Invalid wallet",
    -9: "Account index out of bounds",
    -10: "Address index out of bounds",
    -11: "Invalid key image",
    -12: "Invalid payment ID",
    -13: "Transaction not found",
    -14: "Invalid TX key",
    -15: "No transaction pool",
    -16: "TX too large",
    -17: "Not enough money",
    -18: "TX not possible",
    -19: "Not enough outputs to mix",
    -20: "Not enough unlocked money",
    -21: "Wrong wallet password",
    -22: "Duplicate key images",
    -23: "Unexpected number of outputs",
}


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

    @staticmethod
    def validate_monero_address(address: str) -> Tuple[bool, str]:
        """
        Validate a Monero address format.

        Supports all Monero networks:
        - Mainnet: primary '4', subaddress '8'
        - Testnet: primary '9', subaddress 'A'
        - Stagenet: primary '5', subaddress '7'

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not address:
            return False, "Address is empty"

        # Valid prefixes for all network types
        # Mainnet: 4 (standard), 8 (subaddress)
        # Testnet: 9 (standard), A (subaddress)
        # Stagenet: 5 (standard), 7 (subaddress)
        valid_prefixes = ('4', '8', '9', 'A', '5', '7')
        if address[0] not in valid_prefixes:
            return False, f"Invalid address prefix '{address[0]}' (expected one of: {', '.join(valid_prefixes)})"

        # Standard addresses are 95 chars, integrated addresses are 106 chars
        if len(address) == 95:
            pass  # Standard or subaddress
        elif len(address) == 106:
            pass  # Integrated address
        else:
            return False, f"Invalid address length: {len(address)} (expected 95 or 106)"

        # Check for valid base58 characters
        base58_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        if not all(c in base58_chars for c in address):
            return False, "Address contains invalid characters"

        return True, ""

    def _parse_monero_uri(self, uri: str) -> Tuple[str, int, Optional[str]]:
        """
        Parse a Monero URI into components.

        Args:
            uri: Monero URI (monero:address?params) or plain address

        Returns:
            Tuple of (address, amount_piconero, description)

        Raises:
            MoneroInvalidAddressError: If URI or address is invalid
        """
        address = uri
        amount = 0
        description = None

        if uri.startswith("monero:"):
            parts = uri.replace("monero:", "").split("?")
            address = parts[0]

            if len(parts) > 1:
                try:
                    params = {}
                    for param in parts[1].split("&"):
                        if "=" in param:
                            key, value = param.split("=", 1)
                            params[key] = value

                    if "tx_amount" in params:
                        try:
                            amount = int(float(params["tx_amount"]) * 1e12)
                        except ValueError:
                            raise MoneroInvalidAddressError(f"Invalid amount in URI: {params['tx_amount']}")

                    description = params.get("tx_description")
                except Exception as e:
                    raise MoneroInvalidAddressError(f"Failed to parse Monero URI: {e}")

        # Validate the address
        is_valid, error = self.validate_monero_address(address)
        if not is_valid:
            raise MoneroInvalidAddressError(error)

        return address, amount, description

    async def _rpc_call(
        self,
        method: str,
        params: Optional[dict] = None,
        retries: int = 3,
        retry_delay: float = 1.0
    ) -> dict:
        """
        Make a JSON-RPC call to the Monero wallet with retry logic.

        Args:
            method: RPC method name
            params: Method parameters
            retries: Number of retry attempts
            retry_delay: Delay between retries in seconds

        Returns:
            RPC result dict

        Raises:
            MoneroConnectionError: Unable to connect to wallet RPC
            MoneroTimeoutError: RPC call timed out
            MoneroRPCError: RPC returned an error
            MoneroInsufficientFundsError: Wallet has insufficient funds
        """
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {}
        }

        auth = None
        if self.rpc_username and self.rpc_password:
            auth = httpx.DigestAuth(self.rpc_username, self.rpc_password)

        last_error: Optional[MoneroError] = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        self.rpc_url,
                        json=payload,
                        auth=auth,
                        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
                    )
                    response.raise_for_status()
                    result = response.json()

                    if "error" in result:
                        error = result["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        message = error.get("message", str(error)) if isinstance(error, dict) else str(error)

                        # Map specific error codes to exceptions
                        if code == -17:  # Not enough money
                            raise MoneroInsufficientFundsError(f"Insufficient funds: {message}")
                        elif code == -20:  # Not enough unlocked money
                            raise MoneroInsufficientFundsError(f"Insufficient unlocked funds: {message}")
                        elif code == -7:  # Invalid address
                            raise MoneroInvalidAddressError(f"Invalid address: {message}")
                        elif code in (-18, -19):  # TX not possible
                            raise MoneroTransactionError(f"Transaction error: {message}")
                        else:
                            error_desc = MONERO_RPC_ERRORS.get(code or 0, "Unknown error")
                            raise MoneroRPCError(f"{error_desc}: {message}", code=code)

                    return result.get("result", {})

            except httpx.ConnectError as e:
                last_error = MoneroConnectionError(
                    f"Failed to connect to Monero wallet RPC at {self.rpc_url}: {e}"
                )
                logger.warning(f"Connection attempt {attempt + 1}/{retries} failed: {e}")
            except httpx.TimeoutException as e:
                last_error = MoneroTimeoutError(f"RPC call '{method}' timed out: {e}")
                logger.warning(f"Timeout on attempt {attempt + 1}/{retries}: {e}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise MoneroRPCError("Authentication failed - check RPC username/password", code=401)
                elif e.response.status_code == 403:
                    raise MoneroRPCError("Access forbidden - check RPC configuration", code=403)
                else:
                    last_error = MoneroRPCError(f"HTTP {e.response.status_code}: {e}")
                    logger.warning(f"HTTP error on attempt {attempt + 1}/{retries}: {e}")
            except (MoneroInsufficientFundsError, MoneroInvalidAddressError, MoneroTransactionError, MoneroRPCError):
                # Don't retry these errors
                raise
            except Exception as e:
                last_error = MoneroRPCError(f"Unexpected error: {e}")
                logger.warning(f"Unexpected error on attempt {attempt + 1}/{retries}: {e}")

            if attempt < retries - 1:
                await asyncio.sleep(retry_delay * (attempt + 1))  # Exponential backoff

        # All retries exhausted
        raise last_error or MoneroRPCError("RPC call failed after all retries")

    async def status(self) -> StatusResponse:
        """Get wallet balance and status."""
        try:
            result = await self._rpc_call("get_balance")
            # Balance is in atomic units (piconero)
            balance_atomic = result.get("balance", 0)
            unlocked_balance = result.get("unlocked_balance", 0)

            # Log balance info for debugging
            logger.debug(
                f"Monero wallet status: "
                f"total={balance_atomic / 1e12:.12f} XMR, "
                f"unlocked={unlocked_balance / 1e12:.12f} XMR"
            )

            # Use unlocked balance (spendable)
            balance = Amount(self.unit, unlocked_balance)

            return StatusResponse(balance=balance, error_message=None)

        except MoneroConnectionError as e:
            logger.error(f"Cannot connect to Monero wallet: {e}")
            return StatusResponse(
                balance=Amount(self.unit, 0),
                error_message=f"Wallet connection failed: {e}"
            )
        except MoneroTimeoutError as e:
            logger.error(f"Monero wallet timeout: {e}")
            return StatusResponse(
                balance=Amount(self.unit, 0),
                error_message=f"Wallet timeout: {e}"
            )
        except MoneroError as e:
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
            # Parse and validate the payment request
            try:
                address, parsed_amount, _ = self._parse_monero_uri(quote.request)
            except MoneroInvalidAddressError as e:
                logger.error(f"Invalid payment address: {e}")
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message=f"Invalid Monero address: {e}"
                )

            # Use quote amount, falling back to parsed amount
            amount = quote.amount if quote.amount > 0 else parsed_amount

            if amount <= 0:
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message="Payment amount must be positive"
                )

            # Check wallet balance before attempting payment
            try:
                balance_result = await self._rpc_call("get_balance")
                unlocked_balance = balance_result.get("unlocked_balance", 0)
                estimated_fee = 100000000  # ~0.0001 XMR

                if unlocked_balance < amount + estimated_fee:
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message=f"Insufficient unlocked balance. Need {(amount + estimated_fee) / 1e12:.12f} XMR, have {unlocked_balance / 1e12:.12f} XMR"
                    )
            except MoneroError as e:
                logger.warning(f"Could not check balance before payment: {e}")
                # Continue anyway, let the transfer call fail if needed

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
                    error_message="Transaction broadcast failed: no hash returned"
                )

            # Store payment info
            checking_id = tx_hash
            self.pending_payments[checking_id] = {
                "tx_hash": tx_hash,
                "address": address,
                "amount": amount,
                "fee": fee,
                "status": "broadcast"
            }

            logger.info(f"Sent Monero payment: {tx_hash[:16]}... ({amount / 1e12:.12f} XMR to {address[:16]}...)")

            return PaymentResponse(
                result=PaymentResult.SETTLED,  # Monero txs are instant once broadcast
                checking_id=checking_id,
                fee=Amount(self.unit, fee),
                preimage=tx_hash  # Use tx_hash as preimage equivalent
            )

        except MoneroInsufficientFundsError as e:
            logger.error(f"Insufficient funds for payment: {e}")
            return PaymentResponse(
                result=PaymentResult.FAILED,
                error_message=str(e)
            )
        except MoneroTransactionError as e:
            logger.error(f"Transaction error: {e}")
            return PaymentResponse(
                result=PaymentResult.FAILED,
                error_message=str(e)
            )
        except MoneroConnectionError as e:
            logger.error(f"Connection error during payment: {e}")
            return PaymentResponse(
                result=PaymentResult.UNKNOWN,  # Unknown because we don't know if TX was broadcast
                error_message=f"Connection lost: {e}. Payment status unknown."
            )
        except MoneroError as e:
            logger.error(f"Payment failed: {e}")
            return PaymentResponse(
                result=PaymentResult.FAILED,
                error_message=str(e)
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check if a deposit has been received."""
        if checking_id not in self.pending_deposits:
            logger.debug(f"Unknown invoice checking_id: {checking_id[:16]}...")
            return PaymentStatus(
                result=PaymentResult.UNKNOWN,
                error_message="Unknown invoice - may have expired or been processed"
            )

        deposit = self.pending_deposits[checking_id]

        if deposit.get("paid"):
            return PaymentStatus(
                result=PaymentResult.SETTLED,
                preimage=deposit.get("tx_hash")
            )

        try:
            # Check incoming transfers to this address
            result = await self._rpc_call("get_transfers", {
                "in": True,
                "pending": True,
                "pool": True,
                "subaddr_indices": [deposit["address_index"]]
            })

            # Check all transfer types (in order of confirmation)
            for transfer_type in ["in", "pending", "pool"]:
                transfers = result.get(transfer_type, [])
                for tx in transfers:
                    received_amount = tx.get("amount", 0)
                    expected_amount = deposit["amount"]

                    if received_amount >= expected_amount:
                        # Payment received
                        deposit["paid"] = True
                        deposit["tx_hash"] = tx.get("txid")
                        deposit["confirmations"] = tx.get("confirmations", 0)
                        deposit["received_amount"] = received_amount

                        logger.info(
                            f"Deposit received: {received_amount / 1e12:.12f} XMR "
                            f"(expected {expected_amount / 1e12:.12f} XMR) "
                            f"tx={tx.get('txid', 'unknown')[:16]}..."
                        )

                        # Notify via queue (non-blocking)
                        try:
                            self.paid_invoices_queue.put_nowait(checking_id)
                        except asyncio.QueueFull:
                            logger.warning("Paid invoices queue is full")

                        return PaymentStatus(
                            result=PaymentResult.SETTLED,
                            preimage=tx.get("txid")
                        )

                    elif received_amount > 0:
                        # Partial payment received
                        logger.warning(
                            f"Partial payment received: {received_amount / 1e12:.12f} XMR "
                            f"of {expected_amount / 1e12:.12f} XMR expected"
                        )

            return PaymentStatus(result=PaymentResult.PENDING)

        except MoneroConnectionError as e:
            logger.error(f"Connection error checking invoice: {e}")
            return PaymentStatus(
                result=PaymentResult.UNKNOWN,
                error_message=f"Cannot connect to wallet: {e}"
            )
        except MoneroError as e:
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
        """
        Get a quote for making a payment.

        Raises:
            MoneroInvalidAddressError: If the destination address is invalid
        """
        request = melt_quote.request

        # Parse and validate the request
        address, amount, _ = self._parse_monero_uri(request)

        # Try to get a more accurate fee estimate from the wallet
        estimated_fee = 100000000  # Default: ~0.0001 XMR in piconero
        try:
            # Use estimate_tx_fee if available (requires amount)
            if amount > 0:
                fee_result = await self._rpc_call("estimate_tx_fee", {
                    "destinations": [{"amount": amount, "address": address}],
                    "priority": 1
                }, retries=1)
                if "fee" in fee_result:
                    estimated_fee = fee_result["fee"]
        except MoneroRPCError as e:
            # estimate_tx_fee might not be available in all wallet versions
            logger.debug(f"Could not estimate fee, using default: {e}")
        except Exception:
            pass  # Use default fee

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
        consecutive_errors = 0
        max_consecutive_errors = 5

        while True:
            try:
                pending_count = sum(1 for d in self.pending_deposits.values() if not d.get("paid"))
                if pending_count > 0:
                    logger.debug(f"Polling {pending_count} pending deposits")

                for checking_id, deposit in list(self.pending_deposits.items()):
                    if not deposit.get("paid"):
                        try:
                            await self.get_invoice_status(checking_id)
                        except MoneroError as e:
                            logger.warning(f"Error checking deposit {checking_id[:16]}...: {e}")

                # Reset error counter on success
                consecutive_errors = 0

            except MoneroConnectionError as e:
                consecutive_errors += 1
                logger.error(f"Connection error in payment polling ({consecutive_errors}/{max_consecutive_errors}): {e}")
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("Too many consecutive connection errors, increasing poll interval")
                    await asyncio.sleep(self.poll_interval * 5)
                    continue
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Unexpected error polling payments: {e}")

            await asyncio.sleep(self.poll_interval)
