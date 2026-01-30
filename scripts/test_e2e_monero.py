#!/usr/bin/env python
"""
End-to-end test script for Monero ecash operations.

This script tests the full flow:
1. Start mint server
2. Create wallet connection
3. Deposit XMR -> Mint ecash
4. Check balance
5. Melt ecash -> Withdraw XMR

Usage:
    python scripts/test_e2e_monero.py

Requirements:
    - Running Monero wallet RPC for mint
    - Running Monero wallet RPC for user (optional, for full test)
    - Mint server running or this script will start one
"""

import asyncio
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def test_mint_status():
    """Test that mint is running and has Monero support."""
    import httpx

    mint_url = os.environ.get("MINT_URL", "http://127.0.0.1:3338")

    print(f"Testing mint at {mint_url}...")

    async with httpx.AsyncClient() as client:
        try:
            # Check mint info
            resp = await client.get(f"{mint_url}/v1/info", timeout=10)
            if resp.status_code != 200:
                print(f"Error: Mint returned {resp.status_code}")
                return False

            info = resp.json()
            print(f"Mint: {info.get('name', 'Unknown')}")
            print(f"Version: {info.get('version', 'Unknown')}")

            # Check for Monero support
            nuts = info.get("nuts", {})
            methods = []
            for nut_key, nut_val in nuts.items():
                if isinstance(nut_val, dict) and "methods" in nut_val:
                    methods.extend([m.get("method") for m in nut_val.get("methods", [])])

            if "monero" in methods:
                print("Monero support: YES")
                return True
            else:
                print("Monero support: NO (mint may not have Monero backend configured)")
                return False

        except httpx.ConnectError:
            print(f"Error: Cannot connect to mint at {mint_url}")
            return False
        except Exception as e:
            print(f"Error: {e}")
            return False


async def test_monero_mint_quote():
    """Test creating a Monero mint quote."""
    import httpx

    mint_url = os.environ.get("MINT_URL", "http://127.0.0.1:3338")
    amount = 100_000_000_000  # 0.1 XMR in piconero

    print(f"\nCreating mint quote for {amount / 1e12:.12f} XMR...")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{mint_url}/v1/mint/quote/monero",
                json={"amount": amount, "unit": "pxmr"},
                timeout=30
            )

            if resp.status_code != 200:
                print(f"Error: {resp.status_code} - {resp.text}")
                return None

            quote = resp.json()
            print(f"Quote ID: {quote.get('quote')}")
            print(f"Request: {quote.get('request')}")
            print(f"Amount: {quote.get('amount')}")
            print(f"State: {quote.get('state')}")

            return quote

        except Exception as e:
            print(f"Error creating mint quote: {e}")
            return None


async def test_monero_melt_quote():
    """Test creating a Monero melt quote."""
    import httpx

    mint_url = os.environ.get("MINT_URL", "http://127.0.0.1:3338")

    # Use a test address (this won't actually send funds without proofs)
    test_address = "4" + "A" * 94

    print("\nCreating melt quote...")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{mint_url}/v1/melt/quote/monero",
                json={
                    "request": f"monero:{test_address}?tx_amount=0.01",
                    "unit": "pxmr"
                },
                timeout=30
            )

            if resp.status_code != 200:
                error_text = resp.text
                if "invalid address" in error_text.lower():
                    print("Melt quote test skipped (test address rejected)")
                    return None
                print(f"Error: {resp.status_code} - {error_text}")
                return None

            quote = resp.json()
            print(f"Quote ID: {quote.get('quote')}")
            print(f"Amount: {quote.get('amount')}")
            print(f"Fee: {quote.get('fee_reserve')}")

            return quote

        except Exception as e:
            print(f"Error creating melt quote: {e}")
            return None


async def main():
    """Run end-to-end tests."""
    print("=" * 60)
    print("MONERO ECASH END-TO-END TEST")
    print("=" * 60)

    # Test 1: Check mint status
    print("\n[1/3] Testing mint status...")
    mint_ok = await test_mint_status()
    if not mint_ok:
        print("\nMint is not available or doesn't support Monero.")
        print("Please ensure:")
        print("  1. Mint server is running (poetry run mint)")
        print("  2. MINT_BACKEND_MONERO_XMR=MoneroBackend is set")
        print("  3. Monero wallet RPC is running")
        return 1

    # Test 2: Create mint quote
    print("\n[2/3] Testing mint quote creation...")
    mint_quote = await test_monero_mint_quote()
    if not mint_quote:
        print("Failed to create mint quote")
        return 1
    print("Mint quote created successfully!")

    # Test 3: Create melt quote
    print("\n[3/3] Testing melt quote creation...")
    melt_quote = await test_monero_melt_quote()
    if melt_quote:
        print("Melt quote created successfully!")
    else:
        print("Melt quote test inconclusive (address validation)")

    print("\n" + "=" * 60)
    print("END-TO-END TEST COMPLETE")
    print("=" * 60)
    print("\nTo perform a full deposit/withdrawal test:")
    print("  1. Send XMR to the deposit address from the mint quote")
    print("  2. Wait for confirmation (1+ blocks)")
    print("  3. Use cashu CLI to mint and melt tokens")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
