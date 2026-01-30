#!/usr/bin/env python
"""
Monero-specific CLI commands for Cashu wallet.

These commands handle minting and melting ecash using Monero instead of Lightning.
"""

import asyncio
import sys
from functools import wraps
from typing import Optional

import click
from click import Context
from loguru import logger

from ...core.base import Method, Unit


# Monero address validation
BASE58_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def validate_monero_address(address: str) -> tuple[bool, str]:
    """
    Validate a Monero address.

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
    valid_prefixes = ('4', '8', '9', 'A', '5', '7')
    if address[0] not in valid_prefixes:
        return False, f"Invalid address prefix '{address[0]}'"

    if len(address) == 95:
        pass  # Standard or subaddress
    elif len(address) == 106:
        pass  # Integrated address
    else:
        return False, f"Invalid address length: {len(address)} (expected 95 or 106)"

    if not all(c in BASE58_CHARS for c in address):
        return False, "Address contains invalid characters"

    return True, ""


def format_xmr(piconero: int) -> str:
    """Format piconero amount as XMR string."""
    return f"{piconero / 1e12:.12f}"


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


def print_error(message: str):
    """Print error message in red."""
    click.echo(click.style(f"Error: {message}", fg="red"), err=True)


def print_success(message: str):
    """Print success message in green."""
    click.echo(click.style(message, fg="green"))


def print_warning(message: str):
    """Print warning message in yellow."""
    click.echo(click.style(f"Warning: {message}", fg="yellow"))


def print_info(message: str):
    """Print info message."""
    click.echo(message)


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
        print_error("Wallet not initialized. Please configure your wallet first.")
        return

    # Validate amount
    if amount <= 0:
        print_error("Amount must be positive")
        return

    # Convert XMR to piconero if needed
    if unit.lower() == "xmr":
        amount_atomic = int(amount * 1e12)
        display_amount = f"{amount} XMR"
    elif unit.lower() == "pxmr":
        amount_atomic = int(amount)
        display_amount = f"{amount_atomic} piconero ({format_xmr(amount_atomic)} XMR)"
    else:
        print_error(f"Unknown unit '{unit}'. Use 'xmr' or 'pxmr'")
        return

    print_info(f"Requesting deposit address for {display_amount}...")

    try:
        # Request mint quote from the mint
        quote = await wallet.request_mint(amount_atomic, method=Method.monero)

        # Parse address from request
        request = quote.request
        address = request
        if request.startswith("monero:"):
            address = request.split("?")[0].replace("monero:", "")

        # Display deposit details
        click.echo()
        click.echo("=" * 60)
        print_success("DEPOSIT ADDRESS CREATED")
        click.echo("=" * 60)
        click.echo(f"Amount:   {display_amount}")
        click.echo(f"Quote ID: {quote.quote}")
        click.echo()
        click.echo("Send XMR to this address:")
        click.echo()
        click.echo(click.style(f"  {address}", fg="cyan", bold=True))
        click.echo()
        click.echo("Or use this URI in your wallet:")
        click.echo(f"  {request}")
        click.echo()
        click.echo("After sending XMR, mint your ecash tokens with:")
        click.echo(click.style(f"  cashu monero mint {quote.quote}", fg="yellow"))
        click.echo("=" * 60)

    except ConnectionError as e:
        print_error(f"Cannot connect to mint server: {e}")
        print_info("Please check your mint URL and network connection.")
    except TimeoutError as e:
        print_error(f"Request timed out: {e}")
        print_info("The mint server may be overloaded. Try again later.")
    except Exception as e:
        error_msg = str(e)
        if "not supported" in error_msg.lower():
            print_error("This mint does not support Monero deposits.")
            print_info("Please use a mint that has Monero support enabled.")
        elif "connection" in error_msg.lower():
            print_error(f"Connection error: {e}")
        else:
            print_error(f"Failed to create deposit: {e}")
            logger.debug(f"Full error: {e}")


@monero.command("mint")
@click.argument("quote_id", type=str)
@click.option("--wait", is_flag=True, help="Wait for payment to be confirmed")
@click.pass_context
@coro
async def mint(ctx: Context, quote_id: str, wait: bool):
    """
    Mint ecash tokens after depositing XMR.

    QUOTE_ID: The quote ID from the deposit command

    Example: cashu monero mint abc123...
    """
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        print_error("Wallet not initialized")
        return

    if not quote_id or len(quote_id) < 8:
        print_error("Invalid quote ID")
        return

    print_info(f"Checking payment status for quote {quote_id[:16]}...")

    try:
        # Get the mint quote to check status and get amount
        mint_quote = await wallet.get_mint_quote(quote_id, method=Method.monero)

        if not mint_quote.paid:
            print_warning("Payment not yet received")
            print_info("\nYour XMR deposit has not been detected yet.")
            print_info("Please ensure:")
            print_info("  1. You sent XMR to the correct address")
            print_info("  2. The transaction has at least 1 confirmation")
            print_info("\nTry again in a few minutes.")
            return

        # Mint the tokens
        proofs = await wallet.mint(mint_quote.amount, quote_id, method=Method.monero)

        if not proofs:
            print_warning("No tokens minted. Payment may not have been received yet.")
            print_info("\nPossible reasons:")
            print_info("  1. Payment not yet sent to the deposit address")
            print_info("  2. Payment sent but not yet confirmed (wait for 1+ confirmations)")
            print_info("  3. Payment amount was less than requested")
            print_info("\nTry again after your transaction is confirmed.")
            return

        total = sum(p.amount for p in proofs)
        click.echo()
        click.echo("=" * 60)
        print_success("TOKENS MINTED SUCCESSFULLY")
        click.echo("=" * 60)
        click.echo(f"Tokens:  {len(proofs)} token(s)")
        click.echo(f"Value:   {format_xmr(total)} XMR")
        click.echo(f"         ({total} piconero)")
        click.echo("=" * 60)

    except Exception as e:
        error_msg = str(e).lower()
        if "not paid" in error_msg or "unpaid" in error_msg:
            print_warning("Payment not yet received")
            print_info("\nYour XMR deposit has not been detected yet.")
            print_info("Please ensure:")
            print_info("  1. You sent XMR to the correct address")
            print_info("  2. The transaction has at least 1 confirmation")
            print_info("\nTry again in a few minutes.")
        elif "quote not found" in error_msg or "not found" in error_msg:
            print_error("Quote not found")
            print_info("The quote ID may have expired or is invalid.")
            print_info("Create a new deposit with: cashu monero deposit <amount>")
        elif "already" in error_msg:
            print_warning("Quote already processed")
            print_info("Tokens for this quote may have already been minted.")
            print_info("Check your balance with: cashu monero balance")
        else:
            print_error(f"Minting failed: {e}")
            logger.debug(f"Full error: {e}")


@monero.command("withdraw")
@click.argument("amount", type=float)
@click.argument("address", type=str)
@click.option("--unit", default="xmr", help="Unit (xmr or pxmr)")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
@coro
async def withdraw(ctx: Context, amount: float, address: str, unit: str, force: bool):
    """
    Withdraw XMR by melting ecash tokens.

    AMOUNT: Amount in XMR to withdraw
    ADDRESS: Monero address to receive the XMR

    Example: cashu monero withdraw 0.1 888tNk...
    """
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        print_error("Wallet not initialized")
        return

    # Validate Monero address
    is_valid, error_msg = validate_monero_address(address)
    if not is_valid:
        print_error(f"Invalid Monero address: {error_msg}")
        print_info("\nMonero addresses should:")
        print_info("  - Start with '4' (standard) or '8' (subaddress)")
        print_info("  - Be 95 characters (standard/subaddress) or 106 (integrated)")
        return

    # Validate amount
    if amount <= 0:
        print_error("Amount must be positive")
        return

    # Convert XMR to piconero if needed
    if unit.lower() == "xmr":
        amount_atomic = int(amount * 1e12)
        display_amount = f"{amount} XMR"
    elif unit.lower() == "pxmr":
        amount_atomic = int(amount)
        display_amount = f"{amount_atomic} piconero ({format_xmr(amount_atomic)} XMR)"
    else:
        print_error(f"Unknown unit '{unit}'. Use 'xmr' or 'pxmr'")
        return

    print_info(f"Preparing withdrawal of {display_amount} to {address[:20]}...")

    try:
        # Create melt quote
        monero_uri = f"monero:{address}?tx_amount={amount}"
        quote = await wallet.melt_quote(monero_uri, method=Method.monero)

        fee_xmr = format_xmr(quote.fee_reserve)
        total_xmr = format_xmr(amount_atomic + quote.fee_reserve)

        click.echo()
        click.echo("=" * 60)
        click.echo("WITHDRAWAL QUOTE")
        click.echo("=" * 60)
        click.echo(f"Amount:      {display_amount}")
        click.echo(f"Fee:         {fee_xmr} XMR")
        click.echo(f"Total:       {total_xmr} XMR")
        click.echo(f"To address:  {address[:32]}...")
        click.echo("=" * 60)

        if not force:
            if not click.confirm("Proceed with withdrawal?"):
                print_info("Cancelled.")
                return

        print_info("Processing withdrawal...")

        # Get proofs for the melt (amount + fee)
        total_needed = quote.amount + quote.fee_reserve
        proofs = await wallet.select_to_send(wallet.proofs, total_needed)

        # Execute melt
        result = await wallet.melt(
            proofs,
            monero_uri,
            quote.fee_reserve,
            quote.quote,
            method=Method.monero
        )

        click.echo()
        click.echo("=" * 60)
        print_success("WITHDRAWAL COMPLETE")
        click.echo("=" * 60)
        click.echo(f"Amount:   {display_amount}")
        click.echo(f"To:       {address[:32]}...")
        if result.payment_preimage:
            click.echo(f"TX Hash:  {result.payment_preimage}")
            click.echo()
            click.echo("Track your transaction:")
            click.echo(f"  https://xmrchain.net/tx/{result.payment_preimage}")
        else:
            click.echo("TX Hash:  Processing...")
            print_info("\nTransaction submitted. It may take a moment to appear on chain.")
        click.echo("=" * 60)

    except Exception as e:
        error_msg = str(e).lower()
        if "insufficient" in error_msg or "not enough" in error_msg:
            print_error("Insufficient ecash balance")
            print_info("You don't have enough ecash tokens for this withdrawal.")
            print_info("Check your balance with: cashu monero balance")
        elif "connection" in error_msg:
            print_error(f"Connection error: {e}")
            print_info("Please check your network connection and try again.")
        elif "invalid address" in error_msg:
            print_error("The mint rejected the address as invalid")
        else:
            print_error(f"Withdrawal failed: {e}")
            logger.debug(f"Full error: {e}")


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
        print_error("Wallet not initialized")
        return

    try:
        bal = await wallet.balance()

        # Get Monero balance (piconero unit)
        pxmr_balance = bal.get(Unit.pxmr, 0)
        xmr_balance = format_xmr(pxmr_balance)

        click.echo()
        click.echo("=" * 60)
        click.echo("ECASH BALANCE (Monero)")
        click.echo("=" * 60)

        if pxmr_balance > 0:
            click.echo(click.style(f"  {xmr_balance} XMR", fg="green", bold=True))
        else:
            click.echo(click.style("  0.000000000000 XMR", fg="yellow"))

        click.echo(f"  ({pxmr_balance} piconero)")
        click.echo("=" * 60)

        if pxmr_balance == 0:
            click.echo()
            print_info("To add funds, use: cashu monero deposit <amount>")

    except Exception as e:
        print_error(f"Failed to get balance: {e}")
        logger.debug(f"Full error: {e}")


@monero.command("status")
@click.argument("quote_id", type=str)
@click.pass_context
@coro
async def status(ctx: Context, quote_id: str):
    """
    Check the status of a mint or melt quote.

    QUOTE_ID: The quote ID to check

    Example: cashu monero status abc123...
    """
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        print_error("Wallet not initialized")
        return

    print_info(f"Checking status for {quote_id[:16]}...")

    try:
        # Try to get mint quote status (try Monero first, then bolt11)
        try:
            quote = await wallet.get_mint_quote(quote_id, method=Method.monero)
            click.echo()
            click.echo("=" * 60)
            click.echo("MINT QUOTE STATUS (Monero)")
            click.echo("=" * 60)
            click.echo(f"Quote ID: {quote.quote}")
            click.echo(f"Amount:   {format_xmr(quote.amount)} XMR")
            click.echo(f"State:    {quote.state}")
            if quote.paid:
                print_success("Payment received!")
                print_info("You can now mint tokens with: cashu monero mint " + quote_id)
            else:
                print_warning("Awaiting payment...")
            click.echo("=" * 60)
            return
        except Exception:
            pass

        # Try melt quote (try Monero first)
        try:
            quote = await wallet.get_melt_quote(quote_id, method=Method.monero)
            click.echo()
            click.echo("=" * 60)
            click.echo("MELT QUOTE STATUS (Monero)")
            click.echo("=" * 60)
            click.echo(f"Quote ID: {quote.quote}")
            click.echo(f"Amount:   {format_xmr(quote.amount)} XMR")
            click.echo(f"Fee:      {format_xmr(quote.fee_reserve)} XMR")
            click.echo(f"State:    {quote.state}")
            if quote.paid:
                print_success("Withdrawal complete!")
                if quote.payment_preimage:
                    click.echo(f"TX Hash:  {quote.payment_preimage}")
            click.echo("=" * 60)
            return
        except Exception:
            pass

        print_error("Quote not found")
        print_info("The quote may have expired or the ID is incorrect.")

    except Exception as e:
        print_error(f"Failed to check status: {e}")


# Add to main CLI group
def register_monero_commands(cli):
    """Register Monero commands with the main CLI."""
    cli.add_command(monero)
