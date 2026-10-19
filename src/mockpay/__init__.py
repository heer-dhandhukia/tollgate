"""mockpay: a Razorpay/Stripe-like payment provider used to test Tollgate.

It is deliberately a separate system: its own package, its own database, no imports from
`tollgate`. Tollgate can only see it over HTTP and through webhooks, like a real provider.
"""
