# Stripe billing integration test

The default suite stays offline. One opt-in test exercises a real Stripe
**test-mode** Checkout Session and then checks the application's signed webhook
state transition locally. It creates a Stripe test customer and Checkout
Session but never completes payment or charges a card.

## Configure Stripe test mode

Create a recurring Price in Stripe test mode and provide its ID with a test
secret key. Do not use live credentials:

```bash
export STRIPE_TEST_SECRET_KEY=sk_test_...
export STRIPE_TEST_PRICE_ID=price_...
.venv/bin/pytest -q -m stripe_integration tests/test_stripe_integration.py
```

The test refuses any secret key that does not start with `sk_test_`. Both
variables are required; if neither is set, pytest skips the integration test.
It verifies that the configured Price is active and recurring, creates an
organization through the API, requests a checkout session through the
authenticated billing route, retrieves and checks that session from Stripe,
then applies a locally signed `checkout.session.completed` event and verifies
the tenant's plan changed. Test checkout sessions are expired and test
customers are deleted on cleanup when possible.

The completion event in the pytest integration test is synthetic and signed
locally; it is not sent by Stripe. A separate `stripe-test-mode` CI job runs
only on pushes to `main` when the repository Actions secrets
`STRIPE_TEST_SECRET_KEY` (`sk_test_...`) and `STRIPE_TEST_PRICE_ID` (an active
recurring test Price) are configured. It runs the real Checkout API test, then
starts the API and Stripe CLI listener and triggers a CLI fixture event to
`/api/v1/billing/stripe/webhook`. The CLI supplies the ephemeral signing secret
and forwards the event over HTTP, so this verifies the actual listener,
signature validation, endpoint, and plan update. The CLI-triggered fixture event
is synthetic: it is not the completion event for the Checkout Session created
in the first test, and the Checkout Session is not paid. The job skips when
both secrets are absent and fails if only one is configured.

Never add live credentials. Use a dedicated test account and avoid customer or
production data. CI jobs for pull requests do not receive these secrets.
