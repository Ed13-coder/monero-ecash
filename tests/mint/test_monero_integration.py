"""
Integration tests for Monero backend with real wallet RPC.

These tests require a running Monero wallet RPC server.
Skip these tests if no wallet is available.

To run:
    pytest tests/mint/test_monero_integration.py -v --run-integration

Setup:
    1. Start monero-wallet-rpc:
       monero-wallet-rpc --rpc-bind-port 18082 --wallet-file /path/to/wallet \
           --password yourpassword --disable-rpc-login

    2. Set environment variables (optional):
       export MONERO_RPC_URL=http://127.0.0.1:18082/json_rpc
"""

import asyncio
import os

import pytest

# Check if integration tests should run
MONERO_RPC_URL = os.environ.get("MONERO_RPC_URL", "http://127.0.0.1:18082/json_rpc")
RUN_INTEGRATION = os.environ.get("RUN_MONERO_INTEGRATION", "false").lower() == "true"

# Skip all integration tests if RUN_MONERO_INTEGRATION is not set
pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="Monero integration tests disabled. Set RUN_MONERO_INTEGRATION=true to run."
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: mark test as integration test requiring Monero wallet RPC"
    )


@pytest.fixture
def monero_backend():
    """Create a MoneroBackend instance for testing."""
    from cashu.core.base import Unit
    from cashu.lightning.monero import MoneroBackend

    return MoneroBackend(
        unit=Unit.pxmr,
        rpc_url=MONERO_RPC_URL,
    )


async def check_wallet_available(backend) -> bool:
    """Check if Monero wallet RPC is available."""
    try:
        status = await backend.status()
        return status.error_message is None
    except Exception:
        return False


@pytest.fixture
async def wallet_available(monero_backend):
    """Check if wallet is available and skip if not."""
    available = await check_wallet_available(monero_backend)
    if not available:
        pytest.skip("Monero wallet RPC not available")
    return True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_status(monero_backend, wallet_available):
    """Test getting wallet status."""
    status = await monero_backend.status()

    assert status.error_message is None
    assert status.balance is not None
    assert status.balance.amount >= 0

    print(f"\nWallet balance: {status.balance.amount / 1e12:.12f} XMR")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_subaddress(monero_backend, wallet_available):
    """Test creating a deposit subaddress."""
    from cashu.core.base import Amount, Unit

    amount = Amount(Unit.pxmr, 100_000_000_000)  # 0.1 XMR

    invoice = await monero_backend.create_invoice(amount, memo="integration_test")

    assert invoice.ok
    assert invoice.checking_id is not None
    assert invoice.payment_request is not None
    assert "monero:" in invoice.payment_request

    # Extract address from payment request
    address = invoice.payment_request.split("?")[0].replace("monero:", "")
    is_valid, _ = monero_backend.validate_monero_address(address)
    assert is_valid, "Generated address should be valid"

    print(f"\nCreated deposit address: {address[:32]}...")
    print(f"Checking ID: {invoice.checking_id[:16]}...")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_address_validation_real(monero_backend, wallet_available):
    """Test address validation with real wallet addresses."""
    from cashu.core.base import Amount, Unit

    # Create a real subaddress
    amount = Amount(Unit.pxmr, 1_000_000_000)
    invoice = await monero_backend.create_invoice(amount)

    address = invoice.payment_request.split("?")[0].replace("monero:", "")

    # Validate the generated address
    is_valid, error = monero_backend.validate_monero_address(address)
    assert is_valid, f"Generated address should be valid: {error}"
    assert address.startswith("8"), "Subaddress should start with 8"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_invoice_status_unknown(monero_backend, wallet_available):
    """Test checking status of unknown invoice."""
    from cashu.lightning.base import PaymentResult

    status = await monero_backend.get_invoice_status("unknown_checking_id_12345")

    assert status.result == PaymentResult.UNKNOWN


@pytest.mark.integration
@pytest.mark.asyncio
async def test_payment_quote(monero_backend, wallet_available):
    """Test getting a payment quote for a Monero address."""
    from cashu.core.models import PostMeltQuoteRequest

    # Use a valid testnet address format for quote
    # This is just for quote estimation, no actual payment
    test_address = "4" + "A" * 94  # Placeholder valid format

    melt_request = PostMeltQuoteRequest(
        unit="pxmr",
        request=f"monero:{test_address}?tx_amount=0.001"
    )

    try:
        quote = await monero_backend.get_payment_quote(melt_request)

        assert quote.checking_id is not None
        assert quote.amount.amount == 1_000_000_000  # 0.001 XMR in piconero
        assert quote.fee.amount > 0  # Should have some fee

        print("\nPayment quote:")
        print(f"  Amount: {quote.amount.amount / 1e12:.12f} XMR")
        print(f"  Fee: {quote.fee.amount / 1e12:.12f} XMR")
    except Exception as e:
        if "invalid address" in str(e).lower():
            pytest.skip("Test address rejected by wallet")
        raise


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rpc_connection_error_recovery(monero_backend):
    """Test that backend handles connection errors gracefully."""
    from cashu.lightning.monero import MoneroBackend

    # Create backend with wrong URL
    bad_backend = MoneroBackend(
        rpc_url="http://127.0.0.1:99999/json_rpc"
    )

    status = await bad_backend.status()

    # Should return error status, not raise exception
    assert status.error_message is not None
    assert "connection" in status.error_message.lower() or "connect" in status.error_message.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_invoice_creation(monero_backend, wallet_available):
    """Test creating multiple invoices concurrently."""
    from cashu.core.base import Amount, Unit

    async def create_invoice(amount_pxmr: int):
        amount = Amount(Unit.pxmr, amount_pxmr)
        return await monero_backend.create_invoice(amount)

    # Create 5 invoices concurrently
    amounts = [100_000_000_000, 200_000_000_000, 300_000_000_000, 400_000_000_000, 500_000_000_000]
    tasks = [create_invoice(a) for a in amounts]
    results = await asyncio.gather(*tasks)

    # All should succeed
    for i, invoice in enumerate(results):
        assert invoice.ok, f"Invoice {i} failed: {invoice.error_message}"
        assert invoice.checking_id is not None

    # All checking_ids should be unique
    checking_ids = [inv.checking_id for inv in results]
    assert len(set(checking_ids)) == len(checking_ids), "All checking_ids should be unique"

    print(f"\nCreated {len(results)} concurrent invoices successfully")


# Skip all tests by default unless explicitly enabled
def pytest_collection_modifyitems(config, items):
    if not RUN_INTEGRATION:
        skip_integration = pytest.mark.skip(
            reason="Integration tests disabled. Set RUN_MONERO_INTEGRATION=true to run."
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
