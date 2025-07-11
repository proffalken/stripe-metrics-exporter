#!/usr/bin/env python3
import os
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
)
logger = logging.getLogger(__name__)

# ─── Configure Stripe SDK ──────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_API_KEY")
if not stripe.api_key:
    logger.error("STRIPE_API_KEY environment variable not set")
    raise RuntimeError("STRIPE_API_KEY not set")
logger.info("Stripe SDK configured")

# ─── Prometheus Metrics ────────────────────────────────────────────────────────
active_subs = Gauge(
    "stripe_active_subscriptions",
    "Number of active Stripe subscriptions",
)

# 24 h global charge metrics
payment_count_24h = Gauge(
    "stripe_successful_payments_last_24h",
    "Number of successful Stripe payments in the last 24 hours",
)
total_revenue_24h = Gauge(
    "stripe_total_revenue_last_24h",
    "Total revenue from successful Stripe charges in the last 24 hours (major units)",
)
avg_payment_24h = Gauge(
    "stripe_avg_payment_amount_last_24h",
    "Average Stripe payment amount in the last 24 hours (major units)",
)

# Breakdown of 24 h payments by plan name
payment_count_24h_by_plan = Gauge(
    "stripe_successful_payments_last_24h_by_plan",
    "Number of successful Stripe payments in the last 24 hours, broken down by plan name",
    ["plan_name"],
)
revenue_24h_by_plan = Gauge(
    "stripe_total_revenue_last_24h_by_plan",
    "Total revenue from successful Stripe charges in the last 24 hours, broken down by plan name (major units)",
    ["plan_name"],
)

# Subscription-by-plan metrics remain unchanged
subs_count_by_plan = Gauge(
    "stripe_active_subscriptions_by_plan",
    "Active Stripe subscriptions, broken down by plan name",
    ["plan_name"],
)
subs_mrr_by_plan = Gauge(
    "stripe_subscription_mrr_by_plan",
    "Monthly recurring revenue by subscription plan name (major units)",
    ["plan_name"],
)


# ─── Helpers ───────────────────────────────────────────────────────────────────
def fetch_cycle(window_seconds, count_gauge, revenue_gauge, avg_gauge):
    """Fetch charges in the last `window_seconds` and update global gauges."""
    cutoff = int(time.time()) - window_seconds
    charges = stripe.Charge.list(created={"gte": cutoff}, limit=100)
    successes = [c for c in charges.data if c.paid and c.status == "succeeded"]

    count = len(successes)
    revenue = sum(c.amount / 100.0 for c in successes)
    avg = (revenue / count) if count else 0.0

    count_gauge.set(count)
    revenue_gauge.set(revenue)
    avg_gauge.set(avg)

    return successes  # return the list for plan-breakdown use


def fetch_metrics():
    """Main loop: update subscription and payment metrics every 5 minutes."""
    while True:
        logger.info("Starting metrics fetch cycle")
        try:
            # ─── 1) Subscriptions by plan name ───────────────────────────
            plan_counts = {}
            plan_mrrs = {}
            total_subs = 0
            product_name_cache = {}

            subs_iter = stripe.Subscription.list(
                status="active",
                limit=100,
                expand=["data.items.data.price"]
            ).auto_paging_iter()

            for s in subs_iter:
                total_subs += 1
                for item in s["items"].data:
                    price = item.price
                    plan_name = price.nickname
                    if not plan_name:
                        pid = price.product
                        if pid not in product_name_cache:
                            prod = stripe.Product.retrieve(pid)
                            product_name_cache[pid] = prod.name or pid
                        plan_name = product_name_cache[pid] or price.id

                    qty = item.quantity or 1
                    plan_counts[plan_name] = plan_counts.get(plan_name, 0) + qty
                    unit = (price.unit_amount or 0) / 100.0
                    plan_mrrs[plan_name] = plan_mrrs.get(plan_name, 0.0) + unit * qty

            active_subs.set(total_subs)
            for name, cnt in plan_counts.items():
                subs_count_by_plan.labels(plan_name=name).set(cnt)
            for name, rev in plan_mrrs.items():
                subs_mrr_by_plan.labels(plan_name=name).set(rev)

            logger.info(f"Total active subs: {total_subs}")
            logger.info(f"Subs by plan name: {plan_counts}")
            logger.info(f"MRR by plan name: {plan_mrrs}")

            # ─── 2) 24 h global charges + breakdown by plan ────────────
            successes = fetch_cycle(
                24 * 3600,
                payment_count_24h,
                total_revenue_24h,
                avg_payment_24h,
            )

            # Prepare plan-level counters
            pay_counts = {}
            pay_revenues = {}
            invoice_cache = {}

            for c in successes:
                if not c.invoice:
                    continue
                inv_id = c.invoice
                if inv_id not in invoice_cache:
                    # Expand only lines→price.product (2 levels)
                    invoice_cache[inv_id] = stripe.Invoice.retrieve(
                        inv_id,
                        expand=["lines.data.price", "lines.data.price.product"]
                    )
                inv = invoice_cache[inv_id]
                for line in inv.lines.data:
                    # Only subscription lines
                    if line.type != "subscription":
                        continue
                    price = line.price
                    plan_name = price.nickname or getattr(price.product, "name", None) or price.id

                    qty = line.quantity or 1
                    pay_counts[plan_name] = pay_counts.get(plan_name, 0) + qty
                    unit = (price.unit_amount or 0) / 100.0
                    pay_revenues[plan_name] = pay_revenues.get(plan_name, 0.0) + unit * qty

            # Update the 24h-by-plan metrics
            for name, cnt in pay_counts.items():
                payment_count_24h_by_plan.labels(plan_name=name).set(cnt)
            for name, rev in pay_revenues.items():
                revenue_24h_by_plan.labels(plan_name=name).set(rev)

            logger.info(f"[24h by plan] payments={pay_counts}")
            logger.info(f"[24h by plan] revenue={pay_revenues}")

        except Exception:
            logger.exception("Error during metrics fetch")

        time.sleep(300)  # 5 minutes


# ─── Entrypoint ───────────────────────────────────────────────────────────────
def main():
    start_http_server(8080)
    logger.info("Prometheus HTTP server listening on :8080")

    Thread(target=fetch_metrics, daemon=True).start()
    logger.info("Metrics fetch thread started")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down on interrupt")


if __name__ == "__main__":
    main()

