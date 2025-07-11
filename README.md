# Stripe Metrics Exporter

[![CI Status](https://github.com/proffalken/stripe-metrics-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/proffalken/stripe-metrics-exporter/actions/workflows/ci.yml)
[![Docker Image](https://github.com/proffalken/stripe-metrics-exporter/packages/container/package/stripe-metrics-exporter/badge)](https://github.com/proffalken/stripe-metrics-exporter/packages/container/package/stripe-metrics-exporter)

A lightweight Prometheus exporter that pulls key business metrics from the Stripe API and serves them on `/metrics` for easy scraping by Prometheus (and visualization in Grafana).

---

## 🎯 Features

* **Active subscriptions**
* **Payments in last 24 h**: count, total revenue, average amount
* **Subscription breakdown by plan** (count & MRR)
* **Payments in last 24 h by plan** (count & revenue)
* **Human-friendly plan names** (uses price nickname or product name)
* **Daemonised fetch loop** with 5-minute polling interval
* **Structured logging** for easy debugging

---

## ⚙️ Metrics Exposed

| Metric name                                                  | Description                                                              |
| ------------------------------------------------------------ | ------------------------------------------------------------------------ |
| `stripe_active_subscriptions`                                | Total number of active subscriptions                                     |
| `stripe_successful_payments_last_24h`                        | Number of successful charges in the last 24 hours                        |
| `stripe_total_revenue_last_24h`                              | Total revenue from successful charges in the last 24 hours (major units) |
| `stripe_avg_payment_amount_last_24h`                         | Average payment amount in the last 24 hours (major units)                |
| `stripe_active_subscriptions_by_plan{plan_name="…"}`         | Count of active subscriptions, labelled by plan name                     |
| `stripe_subscription_mrr_by_plan{plan_name="…"}`             | Monthly recurring revenue per plan (major units), labelled by plan name  |
| `stripe_successful_payments_last_24h_by_plan{plan_name="…"}` | Count of successful charges in last 24 h, broken down by plan name       |
| `stripe_total_revenue_last_24h_by_plan{plan_name="…"}`       | Revenue from successful charges in last 24 h, broken down by plan name   |

---

## 🐳 Docker Usage

1. **Pull the image:**

   ```bash
   docker pull ghcr.io/proffalken/stripe-metrics-exporter:latest
   ```
2. **Run with your Stripe key:**

   ```bash
   docker run -d \
     -e STRIPE_API_KEY=sk_test_xxx \
     -p 8080:8080 \
     ghcr.io/proffalken/stripe-metrics-exporter:latest
   ```
3. **Verify:**
   Browse [http://localhost:8080/metrics](http://localhost:8080/metrics) to see Prometheus-formatted metrics.

---

## 🐋 Docker Compose

Create a `docker-compose.yml` alongside:

```yaml
version: "3.7"

services:
  stripe-exporter:
    image: ghcr.io/proffalken/stripe-metrics-exporter:latest
    restart: unless-stopped
    environment:
      - STRIPE_API_KEY=${STRIPE_API_KEY}
    ports:
      - "8080:8080"
```

Then:

```bash
export STRIPE_API_KEY=sk_test_xxx
docker-compose up -d
```

---

## ▶️ Prometheus Scrape Config

Add this job to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'stripe-exporter'
    static_configs:
      - targets: ['<HOST>:8080']
```

Replace `<HOST>` with `localhost` for local dev or your service’s DNS name.

---

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch:

   ```bash
   git checkout -b feature/awesome
   ```
3. Commit your changes:

   ```bash
   git commit -m "Add awesome feature"
   ```
4. Push to your fork:

   ```bash
   git push origin feature/awesome
   ```
5. Open a Pull Request against `proffalken/stripe-metrics-exporter`

Please ensure:

* New functionality is covered by tests
* Code follows existing style (PEP 8 / Black formatting)
* CI passes on GitHub Actions

---

## 📜 License

MIT © [proffalken](https://github.com/proffalken/)

