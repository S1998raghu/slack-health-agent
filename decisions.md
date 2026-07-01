# Architectural Decision Log

## 1. Use MCP (Model Context Protocol) instead of direct API calls

**Decision:** The Slack bot does not call Prometheus directly. Instead, it connects to a separate MCP server that exposes Prometheus tools, and Claude decides which tools to call.

**Why:** MCP separates concerns — the LLM reasoning layer (Claude) is decoupled from the data layer (Prometheus). Adding a new data source means adding a new `@mcp.tool()` without touching the bot. It also means Claude can compose multiple tool calls in one response (e.g., check health AND latency) without the bot hard-coding that logic.

**Alternative considered:** Bot calls Prometheus directly and formats the response itself. Rejected because it removes Claude's reasoning — you get a dashboard, not an agent.

---

## 2. Use SSE transport for MCP (not stdio)

**Decision:** MCP server uses `transport="sse"` and listens on port 8000 over HTTP.

**Why:** stdio transport only works when both processes run on the same machine (parent/child process). In Kubernetes, the bot and MCP server are separate pods — SSE is the only transport that works over the network.

**Gotcha:** FastMCP defaults to listening on `127.0.0.1`, which refuses connections from other pods. Fixed by passing `host="0.0.0.0"` to the `FastMCP()` constructor, not to `mcp.run()`.

---

## 3. Use HotROD as the demo app instead of building a fake metrics generator

**Decision:** Run `jaegertracing/example-hotrod` as the target service that Prometheus scrapes.

**Why:** HotROD is a real Go microservice with real latency variance, real error rates, and real `hotrod_request_latency_*` histogram metrics. This makes the demo credible — the numbers change with traffic, alerts fire on real conditions, and the architecture generalizes to any real service.

**Alternative considered:** Node Exporter (machine metrics). Rejected because CPU/memory metrics are less interesting for a production support demo than request latency and error rates.

---

## 4. Use Claude as the reasoning layer (not rule-based formatting)

**Decision:** Every slash command routes through Claude with a natural language prompt. Claude decides which MCP tools to call and how to format the response.

**Why:** The value of an AI agent over a dashboard is interpretation — Claude can say "the `/route` endpoint is critical, p99 is 2.3s which is above your 1s SLO" rather than just printing a number. It also means the same infrastructure answers open-ended questions in the future.

**Model chosen:** `claude-haiku-4-5-20251001` — fast enough for interactive Slack use, cheap enough for frequent queries.

---

## 5. Slack slash commands use ack() + respond() pattern

**Decision:** Every command handler calls `await ack()` immediately, then does the Claude call, then calls `await respond()`.

**Why:** Slack requires a 200 response within 3 seconds or it shows the user an error. Claude + MCP + Prometheus takes 5-10 seconds. `ack()` sends the immediate 200, `respond()` sends the actual answer as a follow-up message using the response URL.

**Gotcha:** Must use `AsyncApp` and `AsyncSlackRequestHandler` from `slack_bolt.async_app` — the synchronous versions block the event loop and cause timeouts even with ack().

---

## 6. Deploy to GKE Autopilot

**Decision:** Use GKE Autopilot rather than Standard or a VPS.

**Why:** Autopilot manages node provisioning automatically — no need to size node pools or manage OS. For a hackathon, this eliminates an entire class of infrastructure decisions.

**Tradeoffs:** Autopilot blocks access to `kube-system` components (kube-scheduler, etcd, kube-proxy, CoreDNS). When installing `kube-prometheus-stack`, these must be explicitly disabled:
```
--set kubeScheduler.enabled=false
--set kubeControllerManager.enabled=false
--set kubeEtcd.enabled=false
--set kubeProxy.enabled=false
--set coreDns.enabled=false
--set nodeExporter.enabled=false
--set grafana.enabled=false
```

---

## 7. Use kube-prometheus-stack (Helm) for Prometheus

**Decision:** Install Prometheus via the `prometheus-community/kube-prometheus-stack` Helm chart, not a standalone Prometheus deployment.

**Why:** The Helm chart includes the Prometheus Operator, which enables `ServiceMonitor` and `PrometheusRule` CRDs. These let you configure scrape targets and alert rules as Kubernetes resources — no manual prometheus.yml editing.

**Gotcha:** Chart version v3.12.0 uses a distroless Prometheus image that failed on Docker Desktop (`ImageInspectError`). Pinned to `v3.12.0` non-distroless for local dev.

---

## 8. Use ServiceMonitor to tell Prometheus to scrape HotROD

**Decision:** Created a `ServiceMonitor` CRD in the `monitoring` namespace pointing to the `hotrod` service.

**Why:** With the Prometheus Operator, you don't edit `prometheus.yml`. The operator watches `ServiceMonitor` resources and automatically updates Prometheus config. This is the idiomatic Kubernetes-native way.

**Key detail:** The `ServiceMonitor` must be in the same namespace as Prometheus (`monitoring`), or the Prometheus CR must have `serviceMonitorNamespaceSelector` set to match all namespaces.

---

## 9. Use PrometheusRule for alerting, not manual Alertmanager config

**Decision:** Alert rules defined as a `PrometheusRule` CRD, not hardcoded in a ConfigMap.

**Why:** Same reason as ServiceMonitor — the operator picks up changes without restarting Prometheus. Rules are version-controlled as YAML.

**Alerts defined:**
- `HighErrorRate` — endpoint error rate > 5% for 2 minutes
- `HighLatency` — p99 latency > 1s for 2 minutes  
- `ServiceDown` — `up{job="hotrod"} == 0` for 1 minute

---

## 10. Alertmanager webhooks to the Slack bot /alert endpoint

**Decision:** Alertmanager sends fired alerts to `http://<slack-bot-ip>:3000/alert` (a FastAPI endpoint on the bot).

**Why:** This closes the push-based loop — humans don't need to ask `/health`, the system proactively posts to `#oncall` when something fires. The bot endpoint receives the Alertmanager payload, constructs a prompt, and passes it through Claude for an interpreted summary before posting to Slack.

**Alternative considered:** Alertmanager's built-in Slack notifier. Rejected because it just formats the raw alert labels — Claude adds the "what to check first" reasoning.

---

## 11. Secrets via Kubernetes Secret (not CSI Secret Store)

**Decision:** Slack tokens and Anthropic API key are stored in a Kubernetes secret (`slack-bot-secrets`), populated manually from GCP Secret Manager values.

**Why:** CSI Secret Store (secrets-store.csi.k8s.io) failed on GKE Autopilot because the CSI driver DaemonSet could not schedule on Autopilot-managed nodes. The driver pods ran but did not register the node plugin, causing `SecretProviderClass` mounts to hang.

**Alternative attempted:** `SecretSync` controller (new GKE feature with `--enable-secret-manager`). Works for syncing GCP secrets to K8s secrets natively, but required recreating the cluster. For the hackathon timeline, manually creating the K8s secret from GCP Secret Manager values was simpler.

---

## 12. Multi-platform Docker builds for GKE (linux/amd64)

**Decision:** All images built with `docker buildx build --platform linux/amd64`.

**Why:** Local development on Apple Silicon (ARM64). GKE nodes run on x86_64 (AMD64). Without `--platform linux/amd64`, the built images fail with `exec format error` on GKE.

---

## 13. MCP server as a separate deployment, not sidecar

**Decision:** MCP server runs as its own Kubernetes Deployment with a ClusterIP service, not as a sidecar container in the bot pod.

**Why:** Separation of concerns — the MCP server can be updated, scaled, or replaced independently. Other bots or services could connect to the same MCP server. It also makes the architecture diagram cleaner for the hackathon submission.

---

## 15. Use kube-prometheus-stack over Google Managed Prometheus (GMP)

**Decision:** Installed the open-source `prometheus-community/kube-prometheus-stack` Helm chart instead of using GKE's built-in Google Managed Prometheus.

**Why:** GMP has no Alertmanager. When an alert fires in GMP it routes through Google Cloud Alerting, which supports a fixed set of notification channels (email, PagerDuty, etc.) — you cannot point it at an arbitrary webhook URL. The entire push-based flow depends on Alertmanager making an HTTP POST to the bot's `/alert` endpoint, which then passes the alert through Claude before posting to `#oncall`. Without Alertmanager, this flow does not exist.

**Tradeoff:** `kube-prometheus-stack` on GKE Autopilot required disabling several components that Autopilot does not allow (`nodeExporter`, `kubeScheduler`, `kubeEtcd`, `kubeControllerManager`, `kubeProxy`, `coreDns`). GMP would have required zero configuration for scraping but would have eliminated the AI-powered push alerting entirely.

**Rule of thumb:** Use GMP when you only need metrics and dashboards on GKE. Use `kube-prometheus-stack` when you need full Alertmanager control — specifically custom webhooks.

---

## 14. FastAPI as the HTTP layer for the Slack bot

**Decision:** The Slack bot runs on FastAPI (`uvicorn`), with `AsyncSlackRequestHandler` bridging Slack Bolt to FastAPI.

**Why:** Slack Bolt's default HTTP server is single-threaded. FastAPI with uvicorn is async, handles concurrent slash commands, and also serves the `/alert` webhook for Alertmanager — a single process handles both entry points.
