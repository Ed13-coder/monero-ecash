"""Tests for Monero backend."""

import pytest
import respx
from httpx import Response

from cashu.core.base import Amount, MeltQuote, MeltQuoteState, Unit
from cashu.core.models import PostMeltQuoteRequest
from cashu.lightning.monero import MoneroBackend, MoneroRPCError

RPC_URL = "http://127.0.0.1:18082/json_rpc"
monero = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)

VALID_ADDRESS = "4" + "A" * 94
VALID_SUBADDRESS = "8" + "B" * 94
VALID_INTEGRATED = "4" + "C" * 105
VALID_STAGENET_ADDRESS = "5" + "D" * 94
VALID_STAGENET_SUBADDRESS = "7" + "E" * 94
VALID_TESTNET_ADDRESS = "9" + "F" * 94
VALID_TESTNET_SUBADDRESS = "A" + "G" * 94


class TestAddressValidation:
    def test_valid_addresses(self):
        for addr in [VALID_ADDRESS, VALID_SUBADDRESS, VALID_INTEGRATED,
                     VALID_STAGENET_ADDRESS, VALID_STAGENET_SUBADDRESS,
                     VALID_TESTNET_ADDRESS, VALID_TESTNET_SUBADDRESS]:
            is_valid, _ = MoneroBackend.validate_monero_address(addr)
            assert is_valid

    def test_empty_address(self):
        is_valid, error = MoneroBackend.validate_monero_address("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_invalid_prefix(self):
        is_valid, error = MoneroBackend.validate_monero_address("3" + "A" * 94)
        assert not is_valid
        assert "prefix" in error.lower()

    def test_invalid_length(self):
        is_valid, error = MoneroBackend.validate_monero_address("4" + "A" * 50)
        assert not is_valid
        assert "length" in error.lower()

    def test_invalid_characters(self):
        is_valid, error = MoneroBackend.validate_monero_address("4" + "O" * 94)
        assert not is_valid


class TestURIParsing:
    def test_parse_plain_address(self):
        address, amount, desc = monero._parse_monero_uri(VALID_ADDRESS)
        assert address == VALID_ADDRESS
        assert amount == 0

    def test_parse_uri_with_amount(self):
        uri = f"monero:{VALID_ADDRESS}?tx_amount=1.5"
        address, amount, _ = monero._parse_monero_uri(uri)
        assert address == VALID_ADDRESS
        assert amount == 1_500_000_000_000

    def test_parse_invalid_address_raises(self):
        with pytest.raises(MoneroRPCError):
            monero._parse_monero_uri("invalid_address")


@respx.mock
@pytest.mark.asyncio
async def test_monero_status():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "result": {"balance": 5_000_000_000_000, "unlocked_balance": 4_500_000_000_000}
    }))
    status = await monero.status()
    assert status.balance.amount == 4_500_000_000_000


@respx.mock
@pytest.mark.asyncio
async def test_monero_status_error():
    respx.post(RPC_URL).mock(side_effect=Exception("Connection refused"))
    status = await monero.status()
    assert status.balance.amount == 0
    assert status.error_message is not None


@respx.mock
@pytest.mark.asyncio
async def test_monero_create_invoice():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "result": {"address": VALID_SUBADDRESS, "address_index": 1}
    }))
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))
    assert invoice.ok
    assert VALID_SUBADDRESS in invoice.payment_request


@respx.mock
@pytest.mark.asyncio
async def test_monero_create_invoice_failure():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "error": {"code": -8, "message": "Invalid wallet"}
    }))
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))
    assert not invoice.ok


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "result": {"tx_hash": "abc123def456", "fee": 100_000_000}
    }))

    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote", method="monero", checking_id="test",
        unit="pxmr", amount=1_000_000_000_000, fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )
    payment = await monero.pay_invoice(quote, 0)
    assert payment.settled
    assert payment.checking_id == "abc123def456"


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_insufficient_funds():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "error": {"code": -17, "message": "not enough money"}
    }))

    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote", method="monero", checking_id="test",
        unit="pxmr", amount=10_000_000_000_000, fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )
    payment = await monero.pay_invoice(quote, 0)
    assert not payment.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_invalid_address():
    quote = MeltQuote(
        request="invalid_address",
        quote="test_quote", method="monero", checking_id="test",
        unit="pxmr", amount=1_000_000_000_000, fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )
    payment = await monero.pay_invoice(quote, 0)
    assert not payment.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_pending():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "result": {"address": VALID_SUBADDRESS, "address_index": 1}
    }))
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "result": {"in": [], "pending": [], "pool": []}
    }))
    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.pending


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_paid():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "result": {"address": VALID_SUBADDRESS, "address_index": 1}
    }))
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "result": {"in": [{"amount": 1_000_000_000_000, "txid": "tx123"}], "pending": [], "pool": []}
    }))
    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_quote():
    req = PostMeltQuoteRequest(unit=Unit.pxmr.name, request=f"monero:{VALID_ADDRESS}?tx_amount=1.0")
    quote = await monero.get_payment_quote(req)
    assert quote.amount.amount == 1_000_000_000_000


@respx.mock
@pytest.mark.asyncio
async def test_monero_rpc_error():
    respx.post(RPC_URL).mock(return_value=Response(200, json={
        "jsonrpc": "2.0", "id": "0",
        "error": {"code": -17, "message": "not enough money"}
    }))
    with pytest.raises(MoneroRPCError):
        await monero._rpc_call("transfer", {})


@respx.mock
@pytest.mark.asyncio
async def test_monero_auth_error():
    respx.post(RPC_URL).mock(return_value=Response(401))
    with pytest.raises(MoneroRPCError) as exc:
        await monero._rpc_call("get_balance", {}, retries=1)
    assert exc.value.code == 401


class TestUnitConversion:
    def test_xmr_to_piconero(self):
        assert int(1.5 * 1e12) == 1_500_000_000_000

    def test_piconero_to_xmr(self):
        assert 1_500_000_000_000 / 1e12 == 1.5
