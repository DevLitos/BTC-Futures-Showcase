# Security Policy

## Secrets

- Never commit `.env`, `.env.secrets`, API keys, private keys, or exchange credentials.
- Use the example files only as templates.
- Give exchange keys the minimum permissions required and disable withdrawals.
- Rotate a key immediately if it is exposed.

## Live trading

`MODE=live` can submit real orders when `TRADING_ENABLED=true`. Treat that configuration as production-sensitive. Review position size, daily loss limits, exchange permissions, monitoring, and shutdown behavior before using it.

## Reporting a vulnerability

Do not publish credentials or exploit details in a public issue. Contact the repository owner privately with a description, affected files, reproduction steps, and potential impact.