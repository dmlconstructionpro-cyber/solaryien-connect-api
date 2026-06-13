"""
Stripe subscription helpers. Imported lazily so the app boots even when
Stripe isn't configured (endpoints then return 503 "not configured").
"""

import config


def configured():
    return bool(config.STRIPE_SECRET_KEY)


def _stripe():
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def create_checkout_session(pro_id, plan, coverage_type=None,
                            customer_email=None):
    """
    Create a Stripe Checkout Session for a Pro subscription and return its URL.
    The Pro is activated later by the webhook on checkout.session.completed.
    """
    price_id = config.STRIPE_PRICES.get((plan or "").lower())
    if not price_id:
        raise ValueError(f"No Stripe price configured for plan {plan!r}")
    stripe = _stripe()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=config.CHECKOUT_SUCCESS_URL + "?status=success",
        cancel_url=config.CHECKOUT_CANCEL_URL + "?status=cancelled",
        client_reference_id=str(pro_id),
        customer_email=customer_email,
        metadata={"pro_id": str(pro_id), "plan": plan,
                  "coverage_type": coverage_type or ""},
        subscription_data={"metadata": {"pro_id": str(pro_id)}},
    )
    return session.url


def handle_webhook(payload, sig_header):
    """
    Verify a Stripe webhook and, on checkout.session.completed, return the
    pro_id / plan to activate. Returns None for events we ignore.
    """
    stripe = _stripe()
    if config.STRIPE_WEBHOOK_SECRET:
        event = stripe.Webhook.construct_event(
            payload, sig_header, config.STRIPE_WEBHOOK_SECRET)
    else:
        # No signing secret configured (dev only) — parse without verifying.
        import json
        event = json.loads(payload)

    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        return {
            "activate_pro_id": int(s.get("client_reference_id")),
            "plan": (s.get("metadata") or {}).get("plan"),
            "coverage_type": (s.get("metadata") or {}).get("coverage_type"),
        }
    return None
