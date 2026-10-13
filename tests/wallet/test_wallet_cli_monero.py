from types import SimpleNamespace

from click.testing import CliRunner

from cashu.wallet.cli.cli_monero import monero

ADDRESS = "4" + "A" * 94


class StubWallet:
    proofs = ["proof"]

    def __init__(self):
        self.melted_with = None

    async def melt_quote(self, request, method):
        return SimpleNamespace(quote="q", amount=100, fee_reserve=10)

    async def select_to_send(self, proofs, amount, include_fees=False):
        return ["selected"], 0

    async def melt(self, proofs, request, fee_reserve, quote_id, method):
        self.melted_with = proofs
        return SimpleNamespace(payment_preimage="txid")


def test_withdraw_melts_selected_proofs():
    wallet = StubWallet()
    result = CliRunner().invoke(
        monero, ["withdraw", "0.1", ADDRESS, "--force"], obj={"WALLET": wallet}
    )
    assert result.exit_code == 0
    assert wallet.melted_with == ["selected"]
    assert "Sent! TX: txid" in result.output
