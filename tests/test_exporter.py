# tests/test_exporter.py

import os
import sys
import time
import pytest
from prometheus_client import REGISTRY

# Ensure exporter.py is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import exporter

# ─── Dummy classes for subscriptions ─────────────────────────────────────────────
class DummyItem:
    def __init__(
        self,
        price_id="plan_123",
        unit_amount=1000,
        nickname="Test Plan",
        product_id="prod_123",
        quantity=1
    ):
        self.price = type("P", (), {
            "id": price_id,
            "unit_amount": unit_amount,
            "nickname": nickname,
            "product": product_id
        })()
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

class DummyList:
    def __init__(self, subs):
        self._subs = subs

    def auto_paging_iter(self):
        return iter(self._subs)

# ─── Dummy helpers for charges & invoices ───────────────────────────────────────
def make_dummy_charge(amount_cents, paid=True, status="succeeded", invoice="inv_1"):
    c = type("C", (), {})()
    c.amount = amount_cents
    c.paid = paid
    c.status = status
    c.invoice = invoice
    return c

class DummyProduct:
    def __init__(self, name="Test Plan"):
        self.name = name

class DummyPrice:
    def __init__(self, id="plan_123", unit_amount=1000, nickname="Test Plan", product=None):
        self.id = id
        self.unit_amount = unit_amount
        self.nickname = nickname
        self.product = product  # DummyProduct instance

class DummyLine:
    def __init__(self):
        self.type = "subscription"
        prod = DummyProduct(name="Test Plan")
        self.price = DummyPrice(product=prod)
        self.quantity = 1

# ─── Tests ──────────────────────────────────────────────────────────────────────
def test_fetch_metrics_happy_path(monkeypatch):
    # 1) Mock Subscriptions: 3 active subs, each with one DummyItem
    subs = [DummySub([DummyItem()]) for _ in range(3)]
    monkeypatch.setattr(
        exporter.stripe.Subscription,
        "list",
        lambda **kw: DummyList(subs)
    )

    # 2) Mock Charges: two successful charges
    charges = [make_dummy_charge(100), make_dummy_charge(200)]
    monkeypatch.setattr(
        exporter.stripe.Charge,
        "list",
        lambda **kw: type("CL", (), {"data": charges})()
    )

    # 3) Mock Invoice.retrieve to return a dummy invoice with one DummyLine
    dummy_invoice = type("I", (), {
        "lines": type("L", (), {"data": [DummyLine()]})()
    })()
    monkeypatch.setattr(
        exporter.stripe.Invoice,
        "retrieve",
        lambda inv_id, expand=None: dummy_invoice
    )

    # 4) Break out of the loop after one cycle
    monkeypatch.setattr(
        exporter.time,
        "sleep",
        lambda s: (_ for _ in ()).throw(StopIteration)
    )

    # 5) Run and catch StopIteration
    with pytest.raises(StopIteration):
        exporter.fetch_metrics()

    # 6) Assert core metrics
    assert REGISTRY.get_sample_value("stripe_active_subscriptions") == 3.0
    assert REGISTRY.get_sample_value("stripe_successful_payments_last_24h") == 2.0
    assert REGISTRY.get_sample_value("stripe_total_revenue_last_24h") == (100 + 200) / 100.0

    # 7) Assert subscription breakdown by plan
    assert REGISTRY.get_sample_value(
        "stripe_active_subscriptions_by_plan",
        {"plan_name": "Test Plan"}
    ) == 3.0

    # 8) Assert 24h payments-by-plan metrics
    assert REGISTRY.get_sample_value(
        "stripe_successful_payments_last_24h_by_plan",
        {"plan_name": "Test Plan"}
    ) == 2.0
    assert REGISTRY.get_sample_value(
        "stripe_total_revenue_last_24h_by_plan",
        {"plan_name": "Test Plan"}
    ) == 2 * (1000 / 100.0)


def test_fetch_metrics_no_data(monkeypatch):
    # Zero subscriptions
    monkeypatch.setattr(
        exporter.stripe.Subscription,
        "list",
        lambda **kw: DummyList([])
    )
    # Zero charges
    monkeypatch.setattr(
        exporter.stripe.Charge,
        "list",
        lambda **kw: type("CL", (), {"data": []})()
    )
    # Invoice.retrieve should not be called
    monkeypatch.setattr(
        exporter.stripe.Invoice,
        "retrieve",
        lambda inv_id, expand=None: (_ for _ in ()).throw(Exception("Should not be called"))
    )

    # Break loop immediately
    monkeypatch.setattr(
        exporter.time,
        "sleep",
        lambda s: (_ for _ in ()).throw(StopIteration)
    )

    with pytest.raises(StopIteration):
        exporter.fetch_metrics()

    # Core gauges: subscriptions and charges reset to 0
    assert REGISTRY.get_sample_value("stripe_active_subscriptions") == 0.0
    assert REGISTRY.get_sample_value("stripe_successful_payments_last_24h") == 0.0
    assert REGISTRY.get_sample_value("stripe_total_revenue_last_24h") == 0.0

    # Plan-labeled metrics persist (not cleared by code)
    assert REGISTRY.get_sample_value(
        "stripe_active_subscriptions_by_plan",
        {"plan_name": "Test Plan"}
    ) == 3.0
    assert REGISTRY.get_sample_value(
        "stripe_successful_payments_last_24h_by_plan",
        {"plan_name": "Test Plan"}
    ) == 2.0
    assert REGISTRY.get_sample_value(
        "stripe_total_revenue_last_24h_by_plan",
        {"plan_name": "Test Plan"}
    ) == 2 * (1000 / 100.0)

