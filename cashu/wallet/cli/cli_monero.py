"""Monero CLI commands for Cashu wallet."""

import asyncio
from functools import wraps

import click
from click import Context

from ...core.base import Method, Unit
from ...lightning.monero import MoneroBackend


def format_xmr(piconero: int) -> str:
    return f"{piconero / 1e12:.12f}"


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper


def err(msg: str):
    click.echo(click.style(f"Error: {msg}", fg="red"), err=True)


def ok(msg: str):
    click.echo(click.style(msg, fg="green"))


def warn(msg: str):
    click.echo(click.style(msg, fg="yellow"))


@click.group()
def monero():
    """Monero commands."""
    pass


@monero.command("deposit")
@click.argument("amount", type=float)
@click.option("--unit", default="xmr")
@click.pass_context
@coro
async def deposit(ctx: Context, amount: float, unit: str):
    """Request deposit address. AMOUNT in XMR."""
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        err("Wallet not initialized")
        return

    if amount <= 0:
        err("Amount must be positive")
        return

    if unit.lower() == "xmr":
        amount_atomic = int(amount * 1e12)
    else:
        amount_atomic = int(amount)

    try:
        quote = await wallet.request_mint(amount_atomic, method=Method.monero)
        address = quote.request.replace("monero:", "").split("?")[0]

        click.echo(f"\nDeposit {amount} XMR to:")
        click.echo(click.style(address, fg="cyan"))
        click.echo(f"\nThen run: cashu monero mint {quote.quote}")

    except Exception as e:
        err(str(e))


@monero.command("mint")
@click.argument("quote_id", type=str)
@click.pass_context
@coro
async def mint(ctx: Context, quote_id: str):
    """Mint tokens after deposit. QUOTE_ID from deposit command."""
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        err("Wallet not initialized")
        return

    try:
        mint_quote = await wallet.get_mint_quote(quote_id, method=Method.monero)
        if not mint_quote.paid:
            warn("Payment not received yet. Try again after confirmation.")
            return

        proofs = await wallet.mint(mint_quote.amount, quote_id, method=Method.monero)
        if proofs:
            total = sum(p.amount for p in proofs)
            ok(f"Minted {len(proofs)} tokens ({format_xmr(total)} XMR)")
        else:
            warn("No tokens minted")

    except Exception as e:
        err(str(e))


@monero.command("withdraw")
@click.argument("amount", type=float)
@click.argument("address", type=str)
@click.option("--force", "-f", is_flag=True, help="Skip confirmation")
@click.pass_context
@coro
async def withdraw(ctx: Context, amount: float, address: str, force: bool):
    """Withdraw XMR. AMOUNT in XMR, ADDRESS is destination."""
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        err("Wallet not initialized")
        return

    is_valid, error_msg = MoneroBackend.validate_monero_address(address)
    if not is_valid:
        err(f"Invalid address: {error_msg}")
        return

    if amount <= 0:
        err("Amount must be positive")
        return

    try:
        monero_uri = f"monero:{address}?tx_amount={amount}"
        quote = await wallet.melt_quote(monero_uri, method=Method.monero)

        click.echo(f"\nWithdraw {amount} XMR + {format_xmr(quote.fee_reserve)} fee")
        click.echo(f"To: {address[:32]}...")

        if not force and not click.confirm("Proceed?"):
            click.echo("Cancelled")
            return

        total_needed = quote.amount + quote.fee_reserve
        proofs, _ = await wallet.select_to_send(
            wallet.proofs, total_needed, include_fees=True
        )
        result = await wallet.melt(proofs, monero_uri, quote.fee_reserve, quote.quote, method=Method.monero)

        if result.payment_preimage:
            ok(f"Sent! TX: {result.payment_preimage}")
        else:
            ok("Withdrawal submitted")

    except Exception as e:
        err(str(e))


@monero.command("balance")
@click.pass_context
@coro
async def balance(ctx: Context):
    """Show XMR ecash balance."""
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        err("Wallet not initialized")
        return

    try:
        bal = await wallet.balance()
        pxmr = bal.get(Unit.pxmr, 0)
        click.echo(f"Balance: {format_xmr(pxmr)} XMR")
    except Exception as e:
        err(str(e))


@monero.command("status")
@click.argument("quote_id", type=str)
@click.pass_context
@coro
async def status(ctx: Context, quote_id: str):
    """Check quote status."""
    wallet = ctx.obj.get("WALLET")
    if not wallet:
        err("Wallet not initialized")
        return

    # try mint quote
    try:
        quote = await wallet.get_mint_quote(quote_id, method=Method.monero)
        status_str = "paid" if quote.paid else "pending"
        click.echo(f"Mint quote: {format_xmr(quote.amount)} XMR - {status_str}")
        return
    except Exception:
        pass

    # try melt quote
    try:
        quote = await wallet.get_melt_quote(quote_id, method=Method.monero)
        status_str = "complete" if quote.paid else "pending"
        click.echo(f"Melt quote: {format_xmr(quote.amount)} XMR - {status_str}")
        return
    except Exception:
        pass

    err("Quote not found")


def register_monero_commands(cli):
    cli.add_command(monero)
