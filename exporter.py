#!/usr/bin/env python3
import os
import sys
import time
import logging
from threading import Thread

import stripe
from prometheus_client import Gauge, start_http_server

# ─── Configure Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─── Configure Stripe SDK ──────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_API_KEY")
if not stripe.api_key:
    logger.error("STRIPE_API_KEY environment variable not set")
    raise RuntimeError("STRIPE_API_KEY not set")
logger.info("Stripe SDK configured")

# ─── Fee Configuration ─────────────────────────────────────────────────────────
# Stripe fee: percentage (e.g. 0.029 for 2.9%) and flat per-transaction (major units)
FEE_PERCENT = float(os.getenv("STRIPE_FEE_PERCENT", "0.029"))
FEE_FLAT = float(os.getenv("STRIPE_FEE_FLAT", "0.30"))

# ─── Prometheus Metrics ────────────────────────────────────────────────────────
active_subs = Gauge(
    "stripe_active_subscriptions",
    "Number of active Stripe subscriptions"
)

# Subscription-by-plan metrics (gross MRR)
subs_count_by_plan = Gauge(
    "stripe_active_subscriptions_by_plan",
    "Active Stripe subscriptions, broken down by plan name",
    ["plan_name"],
)
subs_mrr_by_plan = Gauge(
    "stripe_subscription_mrr_by_plan",
    "Monthly recurring revenue by subscription plan name (gross, major units)",
    ["plan_name"],
)
# Net MRR after Stripe fees
subs_net_mrr_by_plan = Gauge(
    "stripe_net_subscription_mrr_by_plan",
    "Monthly recurring revenue by subscription plan name (net of Stripe fees, major units)",
    ["plan_name"],
)

# 24h gross payment metrics
payment_count_24h = Gauge(
    "stripe_successful_payments_last_24h",
    "Number of successful Stripe payments in the last 24 hours"
)
total_revenue_24h = Gauge(
    "stripe_total_revenue_last_24h",
    "Total revenue from successful Stripe charges in the last 24 hours (gross, major units)"
)
avg_payment_24h = Gauge(
    "stripe_avg_payment_amount_last_24h",
    "Average Stripe payment amount in the last 24 hours (gross, major units)"
)
# 24h fee & net metrics
fees_24h = Gauge(
    "stripe_fees_last_24h",
    "Total Stripe fees in the last 24 hours (major units)"
)
net_revenue_24h = Gauge(
    "stripe_net_revenue_last_24h",
    "Net revenue (after fees) in the last 24 hours (major units)"
)

# 24h-by-plan charge metrics
payment_count_24h_by_plan = Gauge(
    "stripe_successful_payments_last_24h_by_plan",
    "Number of successful Stripe payments in the last 24 hours, broken down by plan name",
    ["plan_name"],
)
revenue_24h_by_plan = Gauge(
    "stripe_total_revenue_last_24h_by_plan",
    "Total revenue from successful Stripe charges in the last 24 hours, broken down by plan name (gross, major units)",
    ["plan_name"],
)

# Net 24h-by-plan after fees
revenue_net_24h_by_plan = Gauge(
    "stripe_net_revenue_last_24h_by_plan",
    "Net revenue from successful Stripe charges in the last 24 hours, broken down by plan name (net of Stripe fees, major units)",
    ["plan_name"],
)

# ─── Helpers ───────────────────────────────────────────────────────────────────
def fetch_charge_metrics(window_seconds):
    """Fetch charges in last window, calculate gross, fees, and net."""
    cutoff = int(time.time()) - window_seconds
    charges = stripe.Charge.list(
        created={"gte": cutoff}, limit=100,
        expand=["data.balance_transaction"]
    )
    successes = [c for c in charges.data if c.paid and c.status == stripe.ChargeStatusSucceeded]

    gross_cents = sum(c.amount for c in successes)
    fees_cents = sum(c.balance_transaction.fee for c in successes)
    net_cents = sum(c.balance_transaction.net for c in successes)

    payment_count_24h.set(len(successes))
    total_revenue_24h.set(gross_cents / 100.0)
    avg_payment_24h.set((gross_cents / len(successes)) / 100.0 if successes else 0.0)
    fees_24h.set(fees_cents / 100.0)
    net_revenue_24h.set(net_cents / 100.0)

    return successes


def fetch_metrics():
    """Main loop: update all metrics every 5 minutes."""
    while True:
        logger.info("Starting metrics fetch cycle")
        try:
            # Subscriptions & plan breakdown
            counts = {}
            mrrs = {}
            net_mrrs = {}
            total_subs = 0
            product_name_cache = {}

            subs_iter = stripe.Subscription.list(
                status="active", limit=100,
                expand=["data.items.data.price"]
            ).auto_paging_iter()
            for s in subs_iter:
                total_subs += 1
                for item in s["items"].data:
                    price = item.price
                    plan_name = price.nickname or product_name_cache.setdefault(
                        price.product,
                        stripe.Product.retrieve(price.product).name
                    ) or price.id
                    qty = item.quantity or 1
                    gross = (price.unit_amount or 0) * qty / 100.0
                    # calculate net for MRR: gross minus percentage and flat fee
                    net = gross * (1 - FEE_PERCENT) - FEE_FLAT * qty
                    counts[plan_name] = counts.get(plan_name, 0) + qty
                    mrrs[plan_name] = mrrs.get(plan_name, 0.0) + gross
                    net_mrrs[plan_name] = net_mrrs.get(plan_name, 0.0) + net

            active_subs.set(total_subs)
            for name, cnt in counts.items():
                subs_count_by_plan.labels(plan_name=name).set(cnt)
            for name, rev in mrrs.items():
                subs_mrr_by_plan.labels(plan_name=name).set(rev)
            for name, rev in net_mrrs.items():
                subs_net_mrr_by_plan.labels(plan_name=name).set(rev)

            # 24h charge metrics including fees and net
            successes = fetch_charge_metrics(24 * 3600)

            # 24h-by-plan breakdown
            pay_counts = {}
            pay_revenues = {}
            pay_net = {}
            invoice_cache = {}
            for c in successes:
                if not c.invoice:
                    continue
                inv = invoice_cache.get(c.invoice) or stripe.Invoice.retrieve(
                    c.invoice,
                    expand=["lines.data.price", "lines.data.price.product", "payment_intent.payment_method_details.card.issuer"]
                )
                invoice_cache[c.invoice] = inv
                for line in inv.lines.data:
                    if line.type != stripe.InvoiceLineItemTypeSubscription:
                        continue
                    price = line.price
                    plan_name = price.nickname or getattr(price.product, "name", None) or price.id
                    qty = line.quantity or 1
                    gross = (price.unit_amount or 0) * qty / 100.0
                    # find corresponding charge for fee details
                    bt = next(
                        (c.balance_transaction for c in successes if c.invoice == inv.id),
                        None
                    )
                    fee = bt.fee / 100.0 if bt else gross * FEE_PERCENT + FEE_FLAT
                    net = gross - fee
                    pay_counts[plan_name] = pay_counts.get(plan_name, 0) + qty
                    pay_revenues[plan_name] = pay_revenues.get(plan_name, 0.0) + gross
                    pay_net[plan_name] = pay_net.get(plan_name, 0.0) + net

            for name, cnt in pay_counts.items():
                payment_count_24h_by_plan.labels(plan_name=name).set(cnt)
            for name, rev in pay_revenues.items():
                revenue_24h_by_plan.labels(plan_name=name).set(rev)
            for name, rev in pay_net.items():
                revenue_net_24h_by_plan.labels(plan_name=name).set(rev)

        except Exception:
            logger.exception("Error during metrics fetch")

        time.sleep(300)


def main():
    # Bind to all interfaces so Docker port mapping works
    start_http_server(8080, addr="0.0.0.0")
    Thread(target=fetch_metrics, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down on interrupt")


if __name__ == "__main__":
    main()

