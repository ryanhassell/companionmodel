# Twilio Setup

## Required values

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID`

## Webhook endpoints

- SMS inbound: `/webhooks/twilio/sms`
- Status callbacks: `/webhooks/twilio/status`
- Voice TwiML: `/webhooks/twilio/voice`
- Voice status callbacks: `/webhooks/twilio/voice/status`

Set the full public URL through `APP_PUBLIC_WEBHOOK_BASE_URL`.

## Twilio console setup

1. Buy or assign a phone number with SMS and optionally voice.
2. Set the Messaging webhook to `https://your-public-url/webhooks/twilio/sms`.
3. Set status callback URL if you want delivery tracking.
4. For voice, set the voice webhook to `https://your-public-url/webhooks/twilio/voice`.
5. Keep signature validation enabled unless you are in a controlled local dev tunnel.

## MMS notes

- Twilio sends `MediaUrlN` and `MediaContentTypeN`.
- The app stores metadata for inbound media even if no media understanding flow is enabled yet.
