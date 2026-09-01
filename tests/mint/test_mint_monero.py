"""Tests for Monero backend."""

import json

import httpx
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
        for addr in [
            VALID_ADDRESS,
            VALID_SUBADDRESS,
            VALID_INTEGRATED,
            VALID_STAGENET_ADDRESS,
            VALID_STAGENET_SUBADDRESS,
            VALID_TESTNET_ADDRESS,
            VALID_TESTNET_SUBADDRESS,
        ]:
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
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "balance": 5_000_000_000_000,
                    "unlocked_balance": 4_500_000_000_000,
                },
            },
        )
    )
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
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {"address": VALID_SUBADDRESS, "address_index": 1},
            },
        )
    )
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))
    assert invoice.ok
    assert VALID_SUBADDRESS in invoice.payment_request


@respx.mock
@pytest.mark.asyncio
async def test_monero_create_invoice_failure():
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "error": {"code": -8, "message": "Invalid wallet"},
            },
        )
    )
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))
    assert not invoice.ok


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice():
    route = respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200, json={"jsonrpc": "2.0", "id": "0", "result": {"valid": True}}
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "result": {
                        "tx_hash": "abc123def456",
                        "tx_metadata": "signed-transaction",
                        "fee": 100_000_000,
                    },
                },
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "result": {"tx_hash": "abc123def456"},
                },
            ),
        ]
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)

    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test",
        unit="pxmr",
        amount=1_000_000_000_000,
        fee_reserve=200_000_000,
        state=MeltQuoteState.unpaid,
    )
    payment = await backend.pay_invoice(quote, 0)
    assert payment.pending
    assert not payment.settled
    assert payment.checking_id == "abc123def456"
    methods = [json.loads(call.request.content)["method"] for call in route.calls]
    assert methods == ["validate_address", "transfer", "relay_tx"]
    transfer_params = json.loads(route.calls[1].request.content)["params"]
    assert transfer_params["do_not_relay"] is True
    assert transfer_params["get_tx_metadata"] is True


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_insufficient_funds():
    respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200, json={"jsonrpc": "2.0", "id": "0", "result": {"valid": True}}
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "error": {"code": -17, "message": "not enough money"},
                },
            ),
        ]
    )

    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test",
        unit="pxmr",
        amount=10_000_000_000_000,
        fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )
    payment = await monero.pay_invoice(quote, 0)
    assert not payment.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_invalid_address():
    quote = MeltQuote(
        request="invalid_address",
        quote="test_quote",
        method="monero",
        checking_id="test",
        unit="pxmr",
        amount=1_000_000_000_000,
        fee_reserve=100_000_000,
        state=MeltQuoteState.unpaid,
    )
    payment = await monero.pay_invoice(quote, 0)
    assert not payment.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_does_not_retry_ambiguous_transfer():
    route = respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200, json={"jsonrpc": "2.0", "id": "0", "result": {"valid": True}}
            ),
            httpx.ReadTimeout("response lost after request"),
        ]
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test",
        unit="pxmr",
        amount=1_000_000_000_000,
        fee_reserve=200_000_000,
        state=MeltQuoteState.unpaid,
    )

    payment = await backend.pay_invoice(quote, 0)

    assert payment.failed
    methods = [json.loads(call.request.content)["method"] for call in route.calls]
    assert methods == ["validate_address", "transfer"]


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_relay_timeout_stays_pending():
    route = respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200, json={"jsonrpc": "2.0", "id": "0", "result": {"valid": True}}
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "result": {
                        "tx_hash": "tx-timeout",
                        "tx_metadata": "signed-transaction",
                        "fee": 100_000_000,
                    },
                },
            ),
            httpx.ReadTimeout("relay response lost"),
        ]
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test",
        unit="pxmr",
        amount=1_000_000_000_000,
        fee_reserve=200_000_000,
        state=MeltQuoteState.unpaid,
    )

    payment = await backend.pay_invoice(quote, 0)

    assert payment.pending
    assert payment.checking_id == "tx-timeout"
    methods = [json.loads(call.request.content)["method"] for call in route.calls]
    assert methods == ["validate_address", "transfer", "relay_tx"]


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_rejects_fee_above_quote():
    route = respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200, json={"jsonrpc": "2.0", "id": "0", "result": {"valid": True}}
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "result": {
                        "tx_hash": "tx-expensive",
                        "tx_metadata": "signed-transaction",
                        "fee": 300_000_000,
                    },
                },
            ),
        ]
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test",
        unit="pxmr",
        amount=1_000_000_000_000,
        fee_reserve=200_000_000,
        state=MeltQuoteState.unpaid,
    )

    payment = await backend.pay_invoice(quote, 0)

    assert payment.failed
    methods = [json.loads(call.request.content)["method"] for call in route.calls]
    assert methods == ["validate_address", "transfer"]


@respx.mock
@pytest.mark.asyncio
async def test_monero_pay_invoice_does_not_relay_if_state_cannot_be_saved(
    monkeypatch,
):
    route = respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200, json={"jsonrpc": "2.0", "id": "0", "result": {"valid": True}}
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "result": {
                        "tx_hash": "tx-not-saved",
                        "tx_metadata": "signed-transaction",
                        "fee": 100_000_000,
                    },
                },
            ),
        ]
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    monkeypatch.setattr(backend, "_save_state", lambda: False)
    quote = MeltQuote(
        request=f"monero:{VALID_ADDRESS}",
        quote="test_quote",
        method="monero",
        checking_id="test",
        unit="pxmr",
        amount=1_000_000_000_000,
        fee_reserve=200_000_000,
        state=MeltQuoteState.unpaid,
    )

    payment = await backend.pay_invoice(quote, 0)

    assert payment.failed
    assert "tx-not-saved" not in backend.pending_payments
    methods = [json.loads(call.request.content)["method"] for call in route.calls]
    assert methods == ["validate_address", "transfer"]


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_pending():
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {"address": VALID_SUBADDRESS, "address_index": 1},
            },
        )
    )
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {"in": [], "pending": [], "pool": []},
            },
        )
    )
    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.pending


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_paid():
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {"address": VALID_SUBADDRESS, "address_index": 1},
            },
        )
    )
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "in": [
                        {
                            "amount": 1_000_000_000_000,
                            "txid": "tx123",
                            "confirmations": 10,
                            "locked": False,
                        }
                    ],
                    "pending": [],
                    "pool": [],
                },
            },
        )
    )
    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_under_confirmed():
    # A deposit below min_confirmations must NOT be credited: the tx can still
    # be dropped, which would let a depositor mint against money that never lands.
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {"address": VALID_SUBADDRESS, "address_index": 1},
            },
        )
    )
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "in": [
                        {
                            "amount": 1_000_000_000_000,
                            "txid": "tx123",
                            "confirmations": 3,
                        }
                    ]
                },
            },
        )
    )
    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.pending
    assert not status.settled


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lock_fields",
    [
        {"unlock_time": 999_999_999, "locked": True},
        {"unlock_time": 999_999_999},
    ],
    ids=["explicitly-locked", "missing-lock-status"],
)
async def test_monero_get_invoice_status_locked(lock_fields):
    # A transfer can have enough confirmations while a custom unlock_time still
    # prevents the mint from spending it. It must not back liquid ecash yet.
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {"address": VALID_SUBADDRESS, "address_index": 1},
            },
        )
    )
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "in": [
                        {
                            "amount": 1_000_000_000_000,
                            "txid": "tx-locked",
                            "confirmations": 10,
                            **lock_fields,
                        }
                    ]
                },
            },
        )
    )
    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.pending
    assert not status.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_invoice_status_pool_not_credited():
    # A tx still in the txpool (0-conf) must not settle the deposit.
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {"address": VALID_SUBADDRESS, "address_index": 1},
            },
        )
    )
    invoice = await monero.create_invoice(Amount(Unit.pxmr, 1_000_000_000_000))

    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "in": [],
                    "pool": [{"amount": 1_000_000_000_000, "txid": "txpool"}],
                },
            },
        )
    )
    status = await monero.get_invoice_status(invoice.checking_id)
    assert status.pending


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_quote():
    # fee is estimated from a do-not-relay dry-run (+50% headroom), not hardcoded
    respx.post(RPC_URL).mock(
        return_value=Response(
            200, json={"jsonrpc": "2.0", "id": "0", "result": {"fee": 120_000_000}}
        )
    )
    req = PostMeltQuoteRequest(
        unit=Unit.pxmr.name, request=f"monero:{VALID_ADDRESS}?tx_amount=1.0"
    )
    quote = await monero.get_payment_quote(req)
    assert quote.amount.amount == 1_000_000_000_000
    assert quote.fee.amount == 180_000_000


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_status_retries_same_relay_metadata():
    route = respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200, json={"jsonrpc": "2.0", "id": "0", "result": {"tx_hash": "tx123"}}
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "result": {
                        "transfer": {
                            "txid": "tx123",
                            "type": "pending",
                            "confirmations": 0,
                            "fee": 100_000_000,
                        }
                    },
                },
            ),
        ]
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    backend.pending_payments["tx123"] = {
        "tx_hash": "tx123",
        "tx_metadata": "same-signed-transaction",
        "amount": 1_000_000_000_000,
        "fee": 100_000_000,
        "relayed": False,
    }

    status = await backend.get_payment_status("tx123")

    assert status.pending
    methods = [json.loads(call.request.content)["method"] for call in route.calls]
    assert methods == ["relay_tx", "get_transfer_by_txid"]
    assert json.loads(route.calls[0].request.content)["params"]["hex"] == (
        "same-signed-transaction"
    )


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_status_recovers_after_lost_relay_response():
    route = respx.post(RPC_URL).mock(
        side_effect=[
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "error": {"code": -4, "message": "already relayed"},
                },
            ),
            Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "0",
                    "result": {
                        "transfer": {
                            "txid": "tx123",
                            "type": "out",
                            "confirmations": 1,
                            "fee": 100_000_000,
                        }
                    },
                },
            ),
        ]
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    backend.pending_payments["tx123"] = {
        "tx_hash": "tx123",
        "tx_metadata": "same-signed-transaction",
        "amount": 1_000_000_000_000,
        "fee": 100_000_000,
        "relayed": False,
    }

    status = await backend.get_payment_status("tx123")

    assert status.settled
    methods = [json.loads(call.request.content)["method"] for call in route.calls]
    assert methods == ["relay_tx", "get_transfer_by_txid"]


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_status_waits_for_confirmation():
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "transfer": {
                        "txid": "tx123",
                        "type": "pending",
                        "confirmations": 0,
                        "fee": 100_000_000,
                    }
                },
            },
        )
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    backend.pending_payments["tx123"] = {
        "tx_hash": "tx123",
        "tx_metadata": "signed-transaction",
        "amount": 1_000_000_000_000,
        "fee": 100_000_000,
        "relayed": True,
    }

    status = await backend.get_payment_status("tx123")

    assert status.pending
    assert not status.settled


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_status_confirmed_returns_fee():
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "transfer": {
                        "txid": "tx123",
                        "type": "out",
                        "confirmations": 1,
                        "fee": 123_000_000,
                    }
                },
            },
        )
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    backend.pending_payments["tx123"] = {
        "tx_hash": "tx123",
        "tx_metadata": "signed-transaction",
        "amount": 1_000_000_000_000,
        "fee": 100_000_000,
        "relayed": True,
    }

    status = await backend.get_payment_status("tx123")

    assert status.settled
    assert status.preimage == "tx123"
    assert status.fee == Amount(Unit.pxmr, 123_000_000)
    assert "tx123" not in backend.pending_payments


@respx.mock
@pytest.mark.asyncio
async def test_monero_get_payment_status_failed_transfer():
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "result": {
                    "transfer": {
                        "txid": "tx123",
                        "type": "failed",
                        "confirmations": 0,
                    }
                },
            },
        )
    )
    backend = MoneroBackend(unit=Unit.pxmr, rpc_url=RPC_URL)
    backend.pending_payments["tx123"] = {
        "tx_hash": "tx123",
        "tx_metadata": "signed-transaction",
        "amount": 1_000_000_000_000,
        "fee": 100_000_000,
        "relayed": True,
    }

    status = await backend.get_payment_status("tx123")

    assert status.failed
    assert "tx123" not in backend.pending_payments


def test_monero_state_file_is_backward_compatible_and_persists_payments(tmp_path):
    state_file = tmp_path / "monero-state.json"
    legacy_deposits = {
        "invoice": {"address": VALID_SUBADDRESS, "amount": 1, "paid": False}
    }
    state_file.write_text(json.dumps(legacy_deposits))

    backend = MoneroBackend(state_file=str(state_file))
    assert backend.pending_deposits == legacy_deposits
    assert backend.pending_payments == {}

    backend.pending_payments["tx123"] = {
        "tx_hash": "tx123",
        "tx_metadata": "signed-transaction",
        "amount": 1,
        "fee": 1,
        "relayed": False,
    }
    assert backend._save_state()

    restored = MoneroBackend(state_file=str(state_file))
    assert restored.pending_deposits == legacy_deposits
    assert restored.pending_payments == backend.pending_payments


@respx.mock
@pytest.mark.asyncio
async def test_monero_rpc_error():
    respx.post(RPC_URL).mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "0",
                "error": {"code": -17, "message": "not enough money"},
            },
        )
    )
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
