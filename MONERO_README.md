# Cashu Ecash for Monero

This is a fork of [Cashu Nutshell](https://github.com/cashubtc/nutshell) adapted to work with Monero instead of Bitcoin Lightning.

## Bounty

This project is being developed for [Monero Bounty #136](https://bounties.monero.social/posts/136) - Ecash Mint and Wallet for Monero.

## What's Changed

### New Files
- `cashu/lightning/monero.py` - Monero wallet RPC backend
- `cashu/wallet/cli/cli_monero.py` - Monero-specific CLI commands
- `tests/mint/test_mint_monero.py` - Unit tests for Monero backend

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
```

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
- Automatic retry with exponential backoff (3 attempts)
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
- [ ] Integration testing with real Monero wallet RPC
- [ ] End-to-end testing

## License

MIT License (same as original Cashu)
