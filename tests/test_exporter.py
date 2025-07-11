# tests/test_exporter.py

import os
import sys
import time
import pytest
from prometheus_client import REGISTRY

# make exporter.py importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import exporter

def setup_function(fn):
    # ensure fee constants are set predictably
    os.environ["STRIPE_FEE_PERCENT"] = "0.029"
    os.environ["STRIPE_FEE_FLAT"] = "0.30"
    exporter.FEE_PERCENT = float(os.getenv("STRIPE_FEE_PERCENT"))
    exporter.FEE_FLAT = float(os.getenv("STRIPE_FEE_FLAT"))
    # define missing Stripe constants on the module
    setattr(exporter.stripe, "ChargeStatusSucceeded", "succeeded")
    setattr(exporter.stripe, "InvoiceLineItemTypeSubscription", "subscription")


# ─── Helpers ────────────────────────────────────────────────────────────────────

class DummyBalanceTx:
    def __init__(self, fee_cents, net_cents):
        self.fee = fee_cents
        self.net = net_cents

class DummyCharge:
    def __init__(self, amount_cents, fee_cents, net_cents, invoice_id=None):
        self.amount = amount_cents
        self.paid = True
        self.status = exporter.stripe.ChargeStatusSucceeded
        self.balance_transaction = DummyBalanceTx(fee_cents, net_cents)
        self.invoice = invoice_id

class DummyList:
    def __init__(self, seq):
        self._seq = seq
    def auto_paging_iter(self):
        return iter(self._seq)

class DummyPrice:
    def __init__(self, id="plan1", unit_amount=1000, nickname="SubPlan", product=None):
        self.id = id
        self.unit_amount = unit_amount
        self.nickname = nickname
        self.product = product

class DummyItem:
    def __init__(self, price, quantity=1):
        self.price = price
        self.quantity = quantity

class DummySub:
    def __init__(self, items):
        self._items = items
    @property
    def items(self):
        return type("L", (), {"data": self._items})()
    def __getitem__(self, key):
        if key == "items":
            return self.items
        raise KeyError(key)

# ─── Test fetch_charge_metrics ─────────────────────────────────────────────────

def test_fetch_charge_metrics(monkeypatch):
    # Mock two charges: 100¢ (fee 5¢ net 95¢), 200¢ (fee 10¢ net 190¢)
    charges = [
        DummyCharge(100, fee_cents=5,  net_cents=95),
        DummyCharge(200, fee_cents=10, net_cents=190),
    ]
    monkeypatch.setattr(
        exporter.stripe.Charge, "list",
        lambda created, limit, expand: type("CL", (), {"data": charges})()
    )

    # Reset all 24h gauges
    exporter.payment_count_24h.set(0)
    exporter.total_revenue_24h.set(0)
    exporter.avg_payment_24h.set(0)
    exporter.fees_24h.set(0)
    exporter.net_revenue_24h.set(0)

    successes = exporter.fetch_charge_metrics(3600)
    assert successes == charges

    # Verify global 24h metrics
    assert REGISTRY.get_sample_value("stripe_successful_payments_last_24h") == 2.0
    assert pytest.approx(REGISTRY.get_sample_value("stripe_total_revenue_last_24h"), rel=1e-6) == (100 + 200) / 100.0
    # avg = (300/2)/100 = 1.5
    assert pytest.approx(REGISTRY.get_sample_value("stripe_avg_payment_amount_last_24h"), rel=1e-6) == 1.5
    assert pytest.approx(REGISTRY.get_sample_value("stripe_fees_last_24h"), rel=1e-6) == (5 + 10) / 100.0
    assert pytest.approx(REGISTRY.get_sample_value("stripe_net_revenue_last_24h"), rel=1e-6) == (95 + 190) / 100.0


# ─── Test fetch_metrics subscription part ───────────────────────────────────────

def test_fetch_metrics_subscriptions(monkeypatch):
    # 1) Create 2 subscriptions of plan "SubPlan", each with quantity=2
    price = DummyPrice(unit_amount=1000, nickname="SubPlan")
    subs = [DummySub([DummyItem(price, quantity=2)]) for _ in range(2)]
    monkeypatch.setattr(
        exporter.stripe.Subscription, "list",
        lambda status, limit, expand: DummyList(subs)
    )

    # 2) Stub out fetch_charge_metrics so we only test subscription logic
    monkeypatch.setattr(exporter, "fetch_charge_metrics", lambda ws: [])

    # 3) Break out of the infinite loop after first iteration
    monkeypatch.setattr(
        exporter.time, "sleep",
        lambda sec: (_ for _ in ()).throw(StopIteration)
    )

    # Reset subscription-related gauges
    exporter.active_subs.set(0)
    exporter.subs_count_by_plan.labels(plan_name="SubPlan").set(0)
    exporter.subs_mrr_by_plan.labels(plan_name="SubPlan").set(0)
    exporter.subs_net_mrr_by_plan.labels(plan_name="SubPlan").set(0)

    with pytest.raises(StopIteration):
        exporter.fetch_metrics()

    # active_subs = 2 subscriptions total
    assert REGISTRY.get_sample_value("stripe_active_subscriptions") == 2.0

    # subs_count_by_plan = sum of quantities = 2 subs × qty 2 = 4
    assert REGISTRY.get_sample_value(
        "stripe_active_subscriptions_by_plan", {"plan_name": "SubPlan"}
    ) == 4.0

    # gross MRR per sub = (1000¢/100)×2 = 20.0, total = 2×20 = 40.0
    assert pytest.approx(REGISTRY.get_sample_value(
        "stripe_subscription_mrr_by_plan", {"plan_name": "SubPlan"}
    ), rel=1e-6) == 40.0

    # net MRR per sub = gross×(1-FEE_PERCENT) - FEE_FLAT×qty
    per_gross = 20.0
    per_net = per_gross * (1 - exporter.FEE_PERCENT) - exporter.FEE_FLAT * 2
    # total net MRR = 2 subs × per_net
    assert pytest.approx(REGISTRY.get_sample_value(
        "stripe_net_subscription_mrr_by_plan", {"plan_name": "SubPlan"}
    ), rel=1e-6) == 2 * per_net

