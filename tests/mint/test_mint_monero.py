"""
Tests for the Monero backend.

These tests use mocked HTTP responses to test the MoneroBackend class
without requiring a real Monero wallet RPC server.
"""

import pytest
import respx
from httpx import Response

from cashu.core.base import Amount, MeltQuote, MeltQuoteState, Unit
from cashu.core.models import PostMeltQuoteRequest
from cashu.lightning.monero import (
    MoneroBackend,
    MoneroConnectionError,
    MoneroInsufficientFundsError,
    MoneroInvalidAddressError,
    MoneroRPCError,
    MoneroTransactionError,
)

# Test configuration
RPC_URL = "http://127.0.0.1:18082/json_rpc"
monero = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)

# Valid Monero addresses for testing
# Mainnet: primary '4', subaddress '8'
# Testnet: primary '9', subaddress 'A'
# Stagenet: primary '5', subaddress '7'
VALID_ADDRESS = "4" + "A" * 94  # Mainnet standard address
VALID_SUBADDRESS = "8" + "B" * 94  # Mainnet subaddress
VALID_INTEGRATED = "4" + "C" * 105  # Integrated address (106 chars)
VALID_STAGENET_ADDRESS = "5" + "D" * 94  # Stagenet standard address
VALID_STAGENET_SUBADDRESS = "7" + "E" * 94  # Stagenet subaddress
VALID_TESTNET_ADDRESS = "9" + "F" * 94  # Testnet standard address
VALID_TESTNET_SUBADDRESS = "A" + "G" * 94  # Testnet subaddress


class TestAddressValidation:
    """Tests for Monero address validation."""

    def test_valid_mainnet_standard_address(self):
        is_valid, error = MoneroBackend.validate_monero_address(VALID_ADDRESS)
        assert is_valid
        assert error == ""

    def test_valid_mainnet_subaddress(self):
        is_valid, error = MoneroBackend.validate_monero_address(VALID_SUBADDRESS)
        assert is_valid
        assert error == ""

    def test_valid_integrated_address(self):
        is_valid, error = MoneroBackend.validate_monero_address(VALID_INTEGRATED)
        assert is_valid
        assert error == ""

    def test_valid_stagenet_address(self):
        is_valid, error = MoneroBackend.validate_monero_address(VALID_STAGENET_ADDRESS)
        assert is_valid
        assert error == ""

    def test_valid_stagenet_subaddress(self):
        is_valid, error = MoneroBackend.validate_monero_address(VALID_STAGENET_SUBADDRESS)
        assert is_valid
        assert error == ""

    def test_valid_testnet_address(self):
        is_valid, error = MoneroBackend.validate_monero_address(VALID_TESTNET_ADDRESS)
        assert is_valid
        assert error == ""

    def test_valid_testnet_subaddress(self):
        is_valid, error = MoneroBackend.validate_monero_address(VALID_TESTNET_SUBADDRESS)
        assert is_valid
        assert error == ""

    def test_empty_address(self):
        is_valid, error = MoneroBackend.validate_monero_address("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_invalid_prefix(self):
        # '3' is not a valid prefix for any Monero network
        invalid = "3" + "A" * 94
        is_valid, error = MoneroBackend.validate_monero_address(invalid)
        assert not is_valid
        assert "prefix" in error.lower()

    def test_invalid_prefix_b(self):
        # 'B' is not a valid prefix (A is testnet subaddress, not B)
        invalid = "B" + "A" * 94
        is_valid, error = MoneroBackend.validate_monero_address(invalid)
        assert not is_valid
        assert "prefix" in error.lower()

    def test_invalid_length(self):
        invalid = "4" + "A" * 50  # Too short
        is_valid, error = MoneroBackend.validate_monero_address(invalid)
        assert not is_valid
        assert "length" in error.lower()

    def test_invalid_characters(self):
        invalid = "4" + "O" * 94  # 'O' is not in base58
        is_valid, error = MoneroBackend.validate_monero_address(invalid)
        assert not is_valid
        assert "invalid characters" in error.lower()


class TestURIParsing:
    """Tests for Monero URI parsing."""

    def test_parse_plain_address(self):
        address, amount, desc = monero._parse_monero_uri(VALID_ADDRESS)
        assert address == VALID_ADDRESS
        assert amount == 0
        assert desc is None

    def test_parse_uri_with_amount(self):
        uri = f"monero:{VALID_ADDRESS}?tx_amount=1.5"
        address, amount, desc = monero._parse_monero_uri(uri)
        assert address == VALID_ADDRESS
        assert amount == 1_500_000_000_000  # 1.5 XMR in piconero

    def test_parse_uri_with_description(self):
        uri = f"monero:{VALID_ADDRESS}?tx_amount=0.1&tx_description=test_payment"
        address, amount, desc = monero._parse_monero_uri(uri)
        assert address == VALID_ADDRESS
        assert amount == 100_000_000_000  # 0.1 XMR
        assert desc == "test_payment"

    def test_parse_invalid_address_raises(self):
        with pytest.raises(MoneroInvalidAddressError):
            monero._parse_monero_uri("invalid_address")


@respx.mock
@pytest.mark.asyncio
async def test_monero_status():
    """Test wallet status retrieval."""
    mock_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "balance": 5_000_000_000_000,  # 5 XMR
            "unlocked_balance": 4_500_000_000_000,  # 4.5 XMR unlocked
        }
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=mock_response))

    status = await monero.status()
    assert status.balance.amount == 4_500_000_000_000
    assert status.error_message is None


@respx.mock
@pytest.mark.asyncio
async def test_monero_status_connection_error():
    """Test status when wallet is unreachable."""
    respx.post(RPC_URL).mock(side_effect=Exception("Connection refused"))

    status = await monero.status()
    assert status.balance.amount == 0
    assert status.error_message is not None


@respx.mock
@pytest.mark.asyncio
async def test_monero_create_invoice():
    """Test creating a deposit address."""
    mock_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "address": VALID_SUBADDRESS,
            "address_index": 1
        }
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=mock_response))

    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    assert invoice.ok
    assert invoice.checking_id is not None
    assert VALID_SUBADDRESS in invoice.payment_request
    assert "tx_amount=" in invoice.payment_request


@respx.mock
@pytest.mark.asyncio
async def test_monero_create_invoice_failure():
    """Test invoice creation failure."""
    mock_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "error": {"code": -8, "message": "Invalid wallet"}
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=mock_response))

    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    assert not invoice.ok
    assert invoice.error_message is not None


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice():
    """Test sending a payment."""
    # Mock balance check
    balance_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "balance": 10_000_000_000_000,
            "unlocked_balance": 10_000_000_000_000
        }
    }

    # Mock transfer
    transfer_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "tx_hash": "abc123def456",
            "fee": 100_000_000,  # 0.0001 XMR
            "tx_key": "txkey123"
        }
    }

    respx.post(RPC_URL).mock(
        side_effect=[
            Response(200, json=balance_response),
            Response(200, json=transfer_response)
        ]
    )

    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test_checking_id",
        unit="pxmr",
        amount=1_000_000_000_000,  # 1 XMR
        fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )

    payment = await monero.pay_invoice(quote, 0)

    assert payment.settled
    assert payment.checking_id == "abc123def456"
    assert payment.fee is not None
    assert payment.fee.amount == 100_000_000


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_insufficient_funds():
    """Test payment with insufficient funds."""
    mock_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "balance": 100_000_000,
            "unlocked_balance": 100_000_000  # Only 0.0001 XMR
        }
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=mock_response))

    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test_checking_id",
        unit="pxmr",
        amount=10_000_000_000_000,  # 10 XMR - more than balance
        fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )

    payment = await monero.pay_invoice(quote, 0)

    assert not payment.settled
    assert "insufficient" in payment.error_message.lower()


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_invalid_address():
    """Test payment to invalid address."""
    quote = MeltQuote(
        request="invalid_address",
        quote="test_quote",
        method="monero",
        checking_id="test_checking_id",
        unit="pxmr",
        amount=1_000_000_000_000,
        fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )

    payment = await monero.pay_invoice(quote, 0)

    assert not payment.settled
    assert "invalid" in payment.error_message.lower()


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_pending():
    """Test checking invoice status when not yet paid."""
    # First create a pending deposit
    create_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "address": VALID_SUBADDRESS,
            "address_index": 1
        }
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=create_response))
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    # Mock get_transfers with no incoming payments
    transfers_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "in": [],
            "pending": [],
            "pool": []
        }
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=transfers_response))

    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.pending


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_paid():
    """Test checking invoice status when paid."""
    # Create invoice
    create_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "address": VALID_SUBADDRESS,
            "address_index": 1
        }
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=create_response))
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    # Mock get_transfers with incoming payment
    transfers_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "result": {
            "in": [{
                "amount": 1_000_000_000_000,
                "txid": "payment_txid_123",
                "confirmations": 5
            }],
            "pending": [],
            "pool": []
        }
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=transfers_response))

    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.settled
    assert status.preimage == "payment_txid_123"


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_quote():
    """Test getting a payment quote."""
    melt_quote_request = PostMeltQuoteRequest(
        unit=Unit.pxmr.name,
        request=f"monero:{VALID_ADDRESS}?tx_amount=1.0"
    )

    quote = await monero.get_payment_quote(melt_quote_request)

    assert quote.checking_id is not None
    assert quote.amount.amount == 1_000_000_000_000  # 1 XMR in piconero
    assert quote.fee.amount > 0  # Some fee estimate


@respx.mock
@pytest.mark.asyncio
async def test_monero_rpc_error_handling():
    """Test RPC error code mapping."""
    # Test insufficient funds error
    mock_response = {
        "jsonrpc": "2.0",
        "id": "0",
        "error": {"code": -17, "message": "not enough money"}
    }
    respx.post(RPC_URL).mock(return_value=Response(200, json=mock_response))

    with pytest.raises(MoneroInsufficientFundsError):
        await monero._rpc_call("transfer", {})


@respx.mock
@pytest.mark.asyncio
async def test_monero_authentication_error():
    """Test authentication failure handling."""
    respx.post(RPC_URL).mock(return_value=Response(401))

    with pytest.raises(MoneroRPCError) as exc_info:
        await monero._rpc_call("get_balance", {}, retries=1)

    assert exc_info.value.code == 401
    assert "authentication" in str(exc_info.value).lower()


class TestMoneroUnitConversion:
    """Tests for XMR/piconero conversion."""

    def test_xmr_to_piconero(self):
        xmr = 1.5
        piconero = int(xmr * 1e12)
        assert piconero == 1_500_000_000_000

    def test_piconero_to_xmr(self):
        piconero = 1_500_000_000_000
        xmr = piconero / 1e12
        assert xmr == 1.5

    def test_small_amounts(self):
        # 1 piconero is the smallest unit
        piconero = 1
        xmr = piconero / 1e12
        assert xmr == 0.000000000001
