# Polymarket Bot Knowledge

## Current Successful Order Pattern

- A successful terminal result includes `Order placed`, `success: True`, and `status: live`.
- A successful web UI result appears under Portfolio -> Open.
- `0 / 160` means 0 shares filled out of 160 shares total.
- `Until cancelled` means the order is GTC and remains live until filled or cancelled.

## Address Roles

- Rabby / EOA signer address: derived from `PRIVATE_KEY`; signs wallet prompts.
- Polymarket API / funder address: stored in `FUNDER`; for `SIGNATURE_TYPE=3`, order `maker`, `signer`, and `verifyingContract` should match this address.
- Deposit address: used only for adding funds; do not use it as `FUNDER`.

## Rabby TypedDataSign Fields

- `Operation=TypedDataSign`: EIP-712 typed-data signature.
- `contents.salt`: unique order salt / replay protection.
- `maker`: address whose funds/positions back the order.
- `signer`: address the CLOB expects to match the API key identity for the order.
- `tokenId`: outcome token being bought or sold.
- `makerAmount`: BUY side payment amount, usually USDC scaled by 1e6.
- `takerAmount`: BUY side shares amount, usually scaled by 1e6.
- `side`: `0` is BUY, `1` is SELL.
- `signatureType`: `3` is POLY_1271 / deposit wallet.
- `timestamp`: order signing timestamp, usually milliseconds.
- `metadata`: extra order metadata; all zero means none.
- `builder`: builder/referral field; all zero means none.

## Domain Fields

- `name=DepositWallet`: deposit-wallet signing domain.
- `version=1`: signing-domain version.
- `chainId=137`: Polygon mainnet.
- `verifyingContract`: deposit wallet / funder contract that validates the signature.
- `salt`: domain salt; all zero can be normal.
