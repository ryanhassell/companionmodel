# Raspberry Pi Setup

## Recommended hardware

- Raspberry Pi 4 or 5
- 4GB RAM minimum, 8GB preferred
- SSD or high-quality SD card
- UPS or reliable power if always-on

## OS

- Raspberry Pi OS Lite 64-bit
- Update first:

```bash
sudo apt update && sudo apt upgrade -y
```

## Base packages

```bash
sudo apt install -y git curl build-essential libpq-dev
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Deployment options

- Docker Compose: easiest repeatable path
- Native `uv run uvicorn`: lower overhead if you already manage Postgres locally
- `systemd`: use [`scripts/systemd/companion-pi.service`](/Users/cchc/Desktop/companionmodel/scripts/systemd/companion-pi.service)

## Notes

- Keep image generation usage low on the Pi. The Pi orchestrates requests; generation happens in the cloud.
- Use `cloudflared` or a reverse proxy instead of exposing the Pi directly when possible.
