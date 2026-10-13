# Cashu Ecash for Monero

This is a fork of [Cashu Nutshell](https://github.com/cashubtc/nutshell) adapted to work with Monero instead of Bitcoin Lightning.

## Bounty

This project is being developed for [Monero Bounty #136](https://bounties.monero.social/posts/136) - Ecash Mint and Wallet for Monero.

## What's Changed

### New Files
- `cashu/lightning/monero.py` - Monero wallet RPC backend
- `cashu/wallet/cli/cli_monero.py` - Monero-specific CLI commands
- `tests/mint/test_mint_monero.py` - Unit tests for Monero backend
- `tests/mint/test_monero_integration.py` - Integration tests (requires wallet RPC)
- `scripts/test_e2e_monero.py` - End-to-end test script

### Modified Files
- `cashu/core/base.py` - Added `Unit.xmr`, `Unit.pxmr` (piconero), and `Method.monero`
- `cashu/core/settings.py` - Added Monero RPC configuration settings
- `cashu/lightning/__init__.py` - Added MoneroBackend export
- `cashu/mint/startup.py` - Added Monero backend initialization
- `cashu/mint/ledger.py` - Added method parameter support for Monero
- `cashu/mint/router.py` - Added Monero API routes
- `cashu/wallet/cli/cli.py` - Integrated Monero CLI commands

## Configuration

Add to your `.env` file:

```bash
# Monero Backend
MINT_BACKEND_MONERO_XMR=MoneroBackend
MINT_MONERO_RPC_URL=http://127.0.0.1:18082/json_rpc
MINT_MONERO_RPC_USERNAME=  # Optional
MINT_MONERO_RPC_PASSWORD=  # Optional

# Confirmations before a deposit is credited
MINT_MONERO_MIN_CONFIRMATIONS=10

# Optional caps in piconero, leave unset for no limit
MINT_MAX_MINT_MONERO=
MINT_MAX_MELT_MONERO=
```

Deposit and withdrawal tracking is kept in `monero_deposits.json` in `CASHU_DIR`.
If that file cannot be read the mint refuses to start, since starting empty
would forget withdrawals that may already be on chain.

## Requirements

1. Running Monero wallet RPC:
```bash
monero-wallet-rpc --rpc-bind-port 18082 --wallet-file /path/to/wallet --password yourpassword --disable-rpc-login
```

2. Python dependencies:
```bash
pip install -r requirements.txt
```

## How It Works

### Minting (Deposit XMR → Get Ecash)
1. User requests to mint ecash tokens
2. Backend creates a unique Monero subaddress
3. User sends XMR to the subaddress
4. Backend detects payment and issues ecash tokens

### Melting (Redeem Ecash → Get XMR)
1. User submits ecash tokens with a Monero address
2. Backend verifies and burns the tokens
3. Backend sends XMR to the user's address

## Confirmations

### Deposits

A deposit needs `MINT_MONERO_MIN_CONFIRMATIONS` (10 by default) before it counts.
Anything shallower is ignored, including transactions still in the txpool.

Crediting early would let someone mint against a transaction that never lands and
then replace it, which is the same mistake as treating a 0-conf payment as final.

A deposit also has to be reported unlocked by the wallet RPC, because a
transaction can carry an `unlock_time` that keeps its outputs unspendable long
after it is mined. Nodes no longer relay such transactions, but a miner can still
put one in a block. The wallet RPC treats every output as locked for its first
ten blocks anyway, so a confirmation count below 10 does not credit anything
sooner.

### Withdrawals

`transfer` is not idempotent. If the response is lost after the wallet RPC has
sent the transaction, retrying the call pays the address a second time. Withdrawals
are therefore built with `do_not_relay`, saved to the state file together with
the signed transaction, and only then broadcast with `relay_tx`. The build is
never retried, and the relay is only repeated by the status check below.

A withdrawal counts as paid once the daemon has accepted the transaction, not
once it is mined. Waiting for a confirmation would add a couple of minutes to
every melt. It would also cost users their fee change, because the Cashu wallet
does not collect change for a melt that settles after the request returns. The
remaining risk only runs one way: a transaction that was accepted and then never
confirms means the user burned tokens without getting their XMR. It cannot drain
the mint.

If `relay_tx` fails or times out, the melt stays pending. The next status check
asks the wallet RPC about the transaction first and relays the saved copy only if
it has never seen it, because relaying a transaction that is already mined makes
the wallet RPC treat its spent inputs as unspent.

A withdrawal the wallet RPC reports as failed is never rolled back on its own.
It marks a transaction failed when its daemon has not had it in the pool for a
while, and clears that if it reappears, so the transaction can still be mined.
The melt stays pending until the operator has checked the chain and set the
quote with `mint-cli update melt-quote`.

The fee of the built transaction is compared with the reserve quoted to the user
before anything is relayed. If it is higher the transaction is dropped and the
melt fails without spending anything.

## API Endpoints

The following Monero-specific API endpoints are available:

### Mint (Deposit XMR -> Get Ecash)
- `POST /v1/mint/quote/monero` - Request a mint quote (get deposit address)
- `GET /v1/mint/quote/monero/{quote}` - Check mint quote status
- `POST /v1/mint/monero` - Mint tokens after XMR deposit

### Melt (Redeem Ecash -> Get XMR)
- `POST /v1/melt/quote/monero` - Request a melt quote (prepare withdrawal)
- `GET /v1/melt/quote/monero/{quote}` - Check melt quote status
- `POST /v1/melt/monero` - Execute melt (withdraw XMR)

## CLI Commands

```bash
# Deposit XMR to mint ecash
cashu monero deposit 0.1

# Mint ecash after deposit
cashu monero mint <quote_id>

# Check ecash balance
cashu monero balance

# Withdraw XMR
cashu monero withdraw 0.1 <monero_address>

# Check quote status
cashu monero status <quote_id>

# Skip confirmation prompt for withdrawal
cashu monero withdraw 0.1 <address> --force
```

## Error Handling

The Monero backend includes comprehensive error handling:

### Connection Errors
- Retry with backoff (3 attempts), except `transfer` and `relay_tx`, which are sent once
- Clear error messages for connection failures
- Timeout handling for slow RPC responses

### Address Validation
- Validates Monero address format (prefix, length, base58 characters)
- Supports standard (4...), subaddress (8...), and integrated addresses

### Payment Errors
- Pre-flight balance check before sending payments
- Specific error handling for insufficient funds
- Transaction failure detection and reporting

### RPC Error Codes
The backend maps Monero RPC error codes to descriptive messages:
- `-17`: Insufficient funds
- `-20`: Insufficient unlocked funds
- `-7`: Invalid address
- `-18/-19`: Transaction not possible

## Testing

### Unit Tests
Run unit tests (mocked, no wallet needed):
```bash
pytest tests/mint/test_mint_monero.py -v
```

### Integration Tests
Run integration tests with a real Monero wallet RPC:
```bash
# Start Monero wallet RPC first
monero-wallet-rpc --rpc-bind-port 18082 --wallet-file /path/to/wallet --password pass --disable-rpc-login

# Run integration tests
RUN_MONERO_INTEGRATION=true pytest tests/mint/test_monero_integration.py -v
```

### End-to-End Test
Run the full end-to-end test:
```bash
# Start the mint server
poetry run mint

# In another terminal, run the e2e test
python scripts/test_e2e_monero.py
```

## Status

- [x] Monero backend class
- [x] Unit types (XMR, piconero)
- [x] Configuration settings
- [x] Mint startup integration
- [x] API routes for Monero
- [x] Wallet CLI commands
- [x] Ledger method parameter support
- [x] Comprehensive error handling
- [x] Address validation
- [x] Retry logic with exponential backoff
- [x] Wallet API method parameter support
- [x] Full wallet integration (request_mint, mint, melt_quote, melt)
- [x] Unit tests for Monero backend
- [x] Integration test suite (tests/mint/test_monero_integration.py)
- [x] End-to-end test script (scripts/test_e2e_monero.py)
- [ ] Testing with real Monero wallet RPC (testnet/stagenet)

## License

MIT License (same as original Cashu)
