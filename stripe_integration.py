"""
Stripe subscription helpers. Imported lazily so the app boots even when
Stripe isn't configured (endpoints then return 503 "not configured").
"""

import config


def configured():
    return bool(config.STRIPE_SECRET_KEY)


def publishable_key():
    return config.STRIPE_PUBLISHABLE_KEY


def _stripe():
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def _price_id(price_key):
    pid = config.STRIPE_PRICES.get((price_key or "").lower())
    if not pid:
        raise ValueError(f"No Stripe price configured for {price_key!r}")
    return pid


def create_embedded_session(price_key, *, pro_id=None, customer_email=None,
                            quantity=1, metadata=None):
    """
    Create a Stripe Checkout Session in EMBEDDED ui mode and return its
    client_secret. The frontend mounts this inside an in-page modal — no card
    fields ever live on our pages. Subscriptions activate via the webhook;
    one-time charges (bg_check, seat) complete in the same modal.

    price_key is one of the keys in config.STRIPE_PRICES (e.g. apex_professional,
    connect_pro_res, complete_enterprise, bg_check, seat).
    """
    key = (price_key or "").lower()
    price_id = _price_id(key)
    one_time = key in config.STRIPE_ONE_TIME
    md = {"price_key": key}
    if pro_id is not None:
        md["pro_id"] = str(pro_id)
    if metadata:
        md.update({k: str(v) for k, v in metadata.items()})

    stripe = _stripe()
    kwargs = dict(
        ui_mode="embedded",
        line_items=[{"price": price_id, "quantity": quantity}],
        return_url=config.CHECKOUT_RETURN_URL,
        metadata=md,
    )
    if customer_email:
        kwargs["customer_email"] = customer_email
    if pro_id is not None:
        kwargs["client_reference_id"] = str(pro_id)
    if one_time:
        kwargs["mode"] = "payment"
    else:
        kwargs["mode"] = "subscription"
        kwargs["subscription_data"] = {"metadata": md}
    session = stripe.checkout.Session.create(**kwargs)
    return session.client_secret


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
        md = s.get("metadata") or {}
        price_key = md.get("price_key", "")
        ref = s.get("client_reference_id")
        pro_id = int(ref) if ref else None
        # One-time charges (bg_check, seat) must NOT activate a subscription.
        is_one_time = (s.get("mode") == "payment") or price_key in ("bg_check", "seat")
        return {
            "price_key": price_key,
            "pro_id": pro_id,
            # Only subscription completions activate the account.
            "activate_pro_id": None if is_one_time else pro_id,
            "plan": md.get("plan") or price_key,
            "coverage_type": md.get("coverage_type"),
        }
    return None
