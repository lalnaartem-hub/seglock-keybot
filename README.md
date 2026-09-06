# SegLock Pro Telegram KeyBot & Licensing Server

Automated 24/7 HMAC-SHA256 license key generation and subscription bot for SegLock Scooter Unlocker.

## Features
- **HMAC-SHA256 Signature Verification**: Cryptographically secure keys resistant to tampering.
- **Timed Subscriptions**: Flexible pricing plans (1h, 24h, 3d, 7d, 30d, Lifetime).
- **Automated Telegram Bot**: Instant key delivery upon purchase.
- **Key Inspection & Verification**: Integrated validity check engine.

## Deploying to Render.com / Railway.app
1. Connect this GitHub repository.
2. Select **Background Worker** or **Web Service**.
3. Set start command: `python key_bot_server.py`.
4. Deploy!
