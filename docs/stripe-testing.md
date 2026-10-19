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

The signed completion event is synthetic because the test process is private
and does not accept Stripe webhook delivery. For a full delivery-path test,
run the app in a dedicated test environment and forward a test-mode event to the gateway billing webhook route at
`/billing/stripe/webhook`. The standalone public API app does not currently
mount this inbound route, so configure the gateway listener and webhook
endpoint before attempting the delivery test. Never use live-mode or customer
data.
