# obskit Grafana dashboards

Grafana dashboards for the metrics `obskit` emits (v0.3.0+), plus file-based
provisioning examples to wire them onto a fresh Grafana.

These dashboards chart **obskit's own metric names**, not any one application's
metrics, which is why they live in this framework repo: any app that calls
`obskit.setup_metrics(...)` reports the same instruments and works with these
dashboards unchanged. Every panel filters by a `$service` template variable, so
one Grafana can chart many services (or all of them at once).

## Layout

```
grafana/
  dashboards/
    obskit-overview.json    Requests + retrieval: rate, latency, outcome, error/degraded rate
    obskit-genai.json       Model calls: rate, latency, TTFT, token throughput
  provisioning/
    datasources/
      prometheus.example.yaml   Prometheus datasource (uid: prometheus)
    dashboards/
      obskit.example.yaml       File provider that loads dashboards/*.json
```

Built and verified against Grafana 13.1.1 (dashboard `schemaVersion` 39) with a
Prometheus datasource scraping an OpenTelemetry Collector's `prometheus`
exporter.

## Metrics these dashboards expect

Emitted by `obskit.setup_metrics(...)`. Prometheus/OpenMetrics names (dots become
underscores, `.duration` histograms gain a `_seconds` unit suffix):

| obskit instrument | Prometheus series (histogram: `_bucket` / `_count` / `_sum`) |
| --- | --- |
| `lab.request.duration` | `lab_request_duration_seconds*` (labels `lab_route`, `lab_outcome`) |
| `lab.retrieval.duration` | `lab_retrieval_duration_seconds*` |
| `gen_ai.client.operation.duration` | `gen_ai_client_operation_duration_seconds*` (label `gen_ai_request_model`) |
| `gen_ai.client.operation.time_to_first_chunk` | `gen_ai_client_operation_time_to_first_chunk_seconds*` |
| `gen_ai.client.token.usage` | `gen_ai_client_token_usage*` (label `gen_ai_token_type`) |

The `service_name` label comes from the resource (`resource_to_telemetry_conversion`
must be enabled on the Collector's Prometheus exporter, or `service_name` will be
absent and `$service` will have nothing to select).

## Template variables

- **`$datasource`** (type: datasource, query: `prometheus`) — pick which
  Prometheus datasource to chart. Defaults to the datasource with uid
  `prometheus` (what the example datasource file provisions), so the dashboards
  bind out of the box; if your datasource has a different uid, just pick it from
  the dropdown.
- **`$service`** (query: `label_values(service_name)`, multi-value, includes
  "All") — which service(s) to chart. "All" uses `service_name=~".*"`.

## Install on a fresh Grafana (file-based provisioning)

Paths below are the Debian/Ubuntu package defaults
(`/etc/grafana/provisioning`, `/var/lib/grafana`). Adjust for your install
(`[paths] provisioning` in `grafana.ini`).

1. **Datasource.** Copy the example into Grafana's datasource provisioning dir
   and edit `url:` to point at your Prometheus:

   ```
   cp grafana/provisioning/datasources/prometheus.example.yaml \
      /etc/grafana/provisioning/datasources/prometheus.yaml
   # edit url: (default in the file is http://127.0.0.1:9095)
   ```

   Keep `uid: prometheus` so the dashboards' `$datasource` default binds
   automatically. If you already provision a Prometheus datasource, skip this
   and just make sure you select it in the dropdown.

2. **Dashboards.** Copy the JSON somewhere Grafana can read, then install the
   provider that loads it:

   ```
   mkdir -p /var/lib/grafana/dashboards/obskit
   cp grafana/dashboards/*.json /var/lib/grafana/dashboards/obskit/
   chown -R grafana:grafana /var/lib/grafana/dashboards/obskit

   cp grafana/provisioning/dashboards/obskit.example.yaml \
      /etc/grafana/provisioning/dashboards/obskit.yaml
   # the provider's options.path must match where you copied the JSON above
   ```

3. **Load.** Restart Grafana (`systemctl restart grafana-server`) or send it a
   `SIGHUP`. The provider re-reads the JSON every `updateIntervalSeconds` (30s),
   so later dashboard edits do not need a restart.

The dashboards appear in a Grafana folder named **obskit**.

### Provisioning vs. import

These are file-provisioned (read-only in the UI: `allowUiUpdates: false`). To
tinker instead, import the JSON by hand via **Dashboards -> New -> Import** and
pick your datasource when prompted — no provisioning files needed.

## Verifying the panels show real data

Drive a little traffic through an app that has metrics enabled, then open each
dashboard and set `$service`:

- **Rate / latency / token panels** populate once metrics have been scraped
  (allow one export interval plus one scrape, ~up to ~75s after the first
  request).
- **Error & degraded rate** reads **0** when there is traffic but no failing or
  degraded turns. That is correct-empty, shown as a real `0` line (the query
  uses `... or vector(0)`), not "No data". "No data" here means *no traffic at
  all*; a red query error means a broken query — the three are distinguishable.

### Latency percentiles are coarse (known metric limitation)

obskit's duration histograms currently inherit the OpenTelemetry SDK's **default
explicit bucket boundaries** — `0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000,
2500, 5000, 7500, 10000` — which are calibrated for **milliseconds**, while
obskit records durations in **seconds**. Effect on the percentile panels:

- Sub-5-second operations (retrieval, time-to-first-token, fast model calls) all
  fall into the first `[0, 5]` bucket, so `histogram_quantile(0.95, ...)`
  interpolates to ~`0.95 * 5 = 4.75s` regardless of the true value. A ~15ms
  retrieval and a ~600ms TTFT both read ~4.75s at p95.
- Multi-second operations get only coarse resolution (buckets step 5 -> 10 ->
  25 -> 50s), so e.g. a 7s and a 20s request are hard to tell apart.

Because of this, every latency panel also plots an **avg** line computed as
`rate(..._sum) / rate(..._count)`. That average does **not** depend on bucket
boundaries and is exact — prefer it until the buckets are made
seconds-appropriate. The `_count`-based rate panels and the token-throughput
(`_sum`) panels are unaffected. Fixing this means giving the obskit histograms
explicit seconds-scale buckets (a framework metric change), which these
dashboards do not require.
