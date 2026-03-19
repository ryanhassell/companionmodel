# Troubleshooting

## Twilio webhook returns 403

- Check `TWILIO_AUTH_TOKEN`
- Check the exact public URL Twilio is hitting
- Verify proxy headers if using tunnels or reverse proxies

## Messages persist but do not send

- Confirm `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID`
- Check `admin/delivery-failures`
- Check app logs

## No model responses

- Verify `OPENAI_API_KEY`
- Confirm outbound network access
- Inspect logs for provider response errors

## Migrations fail on vector

- Ensure `pgvector` is installed and `CREATE EXTENSION vector` succeeds

## Admin login loops

- Confirm `APP_SECRET_KEY` is set and stable
- If behind HTTPS, review cookie security settings
