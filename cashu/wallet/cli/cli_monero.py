#!/usr/bin/env python
"""
Monero-specific CLI commands for Cashu wallet.

These commands handle minting and melting ecash using Monero instead of Lightning.
"""

import asyncio
from functools import wraps
from typing import Optional

import click
from click import Context
from loguru import logger

from ...core.base import Method, Unit


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


@click.group()
def monero():
    """Monero-specific commands for ecash operations."""
    pass


@monero.command("deposit")
@click.argument("amount", type=float)
@click.option("--unit", default="xmr", help="Unit (xmr or pxmr)")
@click.pass_context
@coro
async def deposit(ctx: Context, amount: float, unit: str):
    """
    Create a deposit address to mint ecash.

    AMOUNT: Amount in XMR to deposit

    Example: cashu monero deposit 0.1
    """
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        click.echo("Error: Wallet not initialized")
        return

    # Convert XMR to piconero if needed
    if unit.lower() == "xmr":
        amount_atomic = int(amount * 1e12)
    else:
        amount_atomic = int(amount)

    click.echo(f"Requesting deposit address for {amount} XMR...")

    try:
        # Request mint quote from the mint
        quote = await wallet.request_mint(amount_atomic, Method.monero, Unit.pxmr)

        # The quote.request contains the Monero address/URI
        click.echo("\n" + "=" * 50)
        click.echo("DEPOSIT DETAILS")
        click.echo("=" * 50)
        click.echo(f"Amount: {amount} XMR ({amount_atomic} piconero)")
        click.echo(f"Quote ID: {quote.quote}")
        click.echo(f"\nSend XMR to:")
        click.echo(f"  {quote.request}")
        click.echo("\nAfter sending, run:")
        click.echo(f"  cashu monero mint {quote.quote}")
        click.echo("=" * 50)

    except Exception as e:
        click.echo(f"Error: {e}")


@monero.command("mint")
@click.argument("quote_id", type=str)
@click.pass_context
@coro
async def mint(ctx: Context, quote_id: str):
    """
    Mint ecash tokens after depositing XMR.

    QUOTE_ID: The quote ID from the deposit command

    Example: cashu monero mint abc123...
    """
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        click.echo("Error: Wallet not initialized")
        return

    click.echo(f"Checking payment status for quote {quote_id[:16]}...")

    try:
        # Check if payment was received and mint tokens
        proofs = await wallet.mint(quote_id, Method.monero, Unit.pxmr)

        total = sum(p.amount for p in proofs)
        click.echo(f"\nSuccess! Minted {len(proofs)} tokens")
        click.echo(f"Total value: {total} piconero ({total / 1e12:.12f} XMR)")

    except Exception as e:
        click.echo(f"Error: {e}")
        click.echo("Payment may not have been received yet. Try again later.")


@monero.command("withdraw")
@click.argument("amount", type=float)
@click.argument("address", type=str)
@click.option("--unit", default="xmr", help="Unit (xmr or pxmr)")
@click.pass_context
@coro
async def withdraw(ctx: Context, amount: float, address: str, unit: str):
    """
    Withdraw XMR by melting ecash tokens.

    AMOUNT: Amount in XMR to withdraw
    ADDRESS: Monero address to receive the XMR

    Example: cashu monero withdraw 0.1 888tNk...
    """
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        click.echo("Error: Wallet not initialized")
        return

    # Validate Monero address (basic check)
    if not address.startswith(("4", "8")):
        click.echo("Error: Invalid Monero address (should start with 4 or 8)")
        return

    if len(address) < 95:
        click.echo("Error: Monero address too short")
        return

    # Convert XMR to piconero if needed
    if unit.lower() == "xmr":
        amount_atomic = int(amount * 1e12)
    else:
        amount_atomic = int(amount)

    click.echo(f"Withdrawing {amount} XMR to {address[:16]}...")

    try:
        # Create melt quote
        quote = await wallet.melt_quote(
            f"monero:{address}?tx_amount={amount}",
            Method.monero,
            Unit.pxmr
        )

        click.echo(f"Fee: {quote.fee_reserve / 1e12:.12f} XMR")

        if not click.confirm("Proceed with withdrawal?"):
            click.echo("Cancelled.")
            return

        # Execute melt
        result = await wallet.melt(quote.quote, Method.monero, Unit.pxmr)

        click.echo("\n" + "=" * 50)
        click.echo("WITHDRAWAL COMPLETE")
        click.echo("=" * 50)
        click.echo(f"Amount: {amount} XMR")
        click.echo(f"To: {address[:32]}...")
        click.echo(f"TX Hash: {result.payment_preimage or 'pending'}")
        click.echo("=" * 50)

    except Exception as e:
        click.echo(f"Error: {e}")


@monero.command("balance")
@click.pass_context
@coro
async def balance(ctx: Context):
    """
    Show ecash balance in XMR.

    Example: cashu monero balance
    """
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        click.echo("Error: Wallet not initialized")
        return

    try:
        bal = await wallet.balance()

        # Get Monero balance (piconero unit)
        pxmr_balance = bal.get(Unit.pxmr, 0)
        xmr_balance = pxmr_balance / 1e12

        click.echo("\n" + "=" * 50)
        click.echo("ECASH BALANCE")
        click.echo("=" * 50)
        click.echo(f"  {xmr_balance:.12f} XMR")
        click.echo(f"  ({pxmr_balance} piconero)")
        click.echo("=" * 50)

    except Exception as e:
        click.echo(f"Error: {e}")


# Add to main CLI group
def register_monero_commands(cli):
    """Register Monero commands with the main CLI."""
    cli.add_command(monero)
