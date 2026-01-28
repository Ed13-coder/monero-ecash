# Cashu Ecash for Monero

This is a fork of [Cashu Nutshell](https://github.com/cashubtc/nutshell) adapted to work with Monero instead of Bitcoin Lightning.

## Bounty

This project is being developed for [Monero Bounty #136](https://bounties.monero.social/posts/136) - Ecash Mint and Wallet for Monero.

## What's Changed

### New Files
- `cashu/lightning/monero.py` - Monero wallet RPC backend

### Modified Files
- `cashu/core/base.py` - Added `Unit.xmr`, `Unit.pxmr` (piconero), and `Method.monero`
- `cashu/core/settings.py` - Added Monero RPC configuration settings
- `cashu/lightning/__init__.py` - Added MoneroBackend export
- `cashu/mint/startup.py` - Added Monero backend initialization

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

## Status

🚧 **Work in Progress**

- [x] Monero backend class
- [x] Unit types (XMR, piconero)
- [x] Configuration settings
- [x] Mint startup integration
- [ ] Full integration testing
- [ ] Wallet CLI updates
- [ ] Documentation

## License

MIT License (same as original Cashu)
