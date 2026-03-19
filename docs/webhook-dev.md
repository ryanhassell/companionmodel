# Local Webhook Development

## ngrok

```bash
ngrok http 8000
```

Set:

```bash
APP_PUBLIC_WEBHOOK_BASE_URL=https://your-ngrok-subdomain.ngrok-free.app
```

## cloudflared

```bash
cloudflared tunnel --url http://localhost:8000
```

## Notes

- Update Twilio webhook URLs every time the tunnel URL changes unless you use a reserved domain.
- In dev, you may temporarily disable strict signature validation in `config/defaults.yaml`, but turn it back on for production.
