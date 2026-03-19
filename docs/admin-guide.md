# Admin Guide

## Main pages

- `Overview`: counts and job runs
- `Users`: phone records, notes, overrides
- `Personas`: companion identity and boundaries
- `Messages`: recent inbound/outbound history
- `Memory`: editable durable memory
- `Vector Search`: inspect retrieval hits
- `Schedules`: proactive and quiet-hour windows
- `Prompts`: versioned prompt bodies
- `Safety`: distress and policy events
- `Media`: generate and inspect images
- `Test Tools`: send test SMS, preview prompts, trigger proactive messages, place test calls
- `Settings`: DB-backed runtime overrides

## Recommended first-run flow

1. Create admin account.
2. Seed the example persona and prompt templates.
3. Edit the example persona for the real user.
4. Add the user phone number.
5. Review `config/defaults.yaml` safety caps.
6. Configure Twilio and OpenAI.
7. Send a test message from the admin tools page.
