# Slack Health Agent

An AI-powered production support agent for Slack. Ask it about service health and errors — or let it page your team automatically when something breaks.

Built for the [Slack Agent Builder Challenge](https://slackhack.devpost.com) — New Slack Agent track.

---

## What it does

**Slash commands (pull-based):**
- `/health` — Claude checks service status, error rates, worst latency, and flags what to watch out for
- `/top` — Claude ranks the top 5 endpoints by error rate with p99 latency and attention level

**Proactive alerts (push-based):**
- Prometheus evaluates alert rules every 15 seconds
- When `HighErrorRate`, `HighLatency`, or `ServiceDown` fires, Alertmanager webhooks the bot
- Claude posts a structured diagnosis to `#oncall` — alert, cause, and suggested fix — before any human notices

**Multi-service ready:**
- Set the `SERVICE_NAME` env var to onboard any new service
- Every `#oncall` message is prefixed with the service name so multiple services share one channel

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        SLACK                                 │
│  Engineer types /health or /top          #oncall channel    │
└──────────────┬──────────────────────────────────▲───────────┘
               │                                  │
               ▼                                  │
┌──────────────────────────┐                      │
│  Slack Bot               │                      │
│  FastAPI + Slack Bolt    │──────────────────────┘
│  /slack/{command}        │   chat_postMessage
│  /alert  (webhook)       │
└──────────┬───────────────┘
           │ ask_claude()
           ▼
┌──────────────────────────┐
│  Claude Haiku            │
│  Anthropic SDK           │
│  Decides which tools     │
│  to call                 │
└──────────┬───────────────┘
           │ MCP tool calls over SSE
           ▼
┌──────────────────────────┐
│  MCP Server              │
│  FastMCP (SSE)           │
│  get_service_health      │
│  get_top_errors          │
│  get_latency             │
└──────────┬───────────────┘
           │ PromQL HTTP API
           ▼
┌──────────────────────────┐     scrapes every 15s
│  Prometheus              │◄───────────────────────── HotROD
│  + Alertmanager          │                        (demo microservice)
│  kube-prometheus-stack   │
└──────────┬───────────────┘
           │ webhook on alert firing
           └──────────────────────► Slack Bot /alert
```

All services run on **GKE Autopilot**. Only the Slack bot and HotROD have public LoadBalancer IPs. Everything else communicates internally over Kubernetes DNS.

---

## Stack

| Layer | Technology |
|---|---|
| AI reasoning | Claude Haiku (`claude-haiku-4-5-20251001`) via Anthropic SDK |
| Tool protocol | MCP (Model Context Protocol) with SSE transport |
| Slack integration | Slack Bolt async + FastAPI + uvicorn |
| Metrics | Prometheus + kube-prometheus-stack Helm chart |
| Alerting | Alertmanager with webhook receiver |
| Demo app | HotROD (`jaegertracing/example-hotrod`) |
| Infrastructure | GKE Autopilot |
| Container registry | Google Artifact Registry |

---

## Slash command flow

1. Engineer types `/health` in Slack
2. Bot calls `await ack()` immediately — beats Slack's 3s timeout
3. Bot sends a structured prompt to Claude
4. Claude calls MCP tools (`get_service_health`, `get_top_errors`, `get_latency`) as needed
5. MCP server queries Prometheus with PromQL and returns results
6. Claude formats a fixed-structure response
7. Bot calls `await respond()` with the answer

---

## Push alert flow

1. Prometheus evaluates `HighErrorRate` / `HighLatency` / `ServiceDown` rules every 15s
2. Alert moves `inactive → pending → firing` when condition is sustained
3. Prometheus notifies Alertmanager
4. Alertmanager POSTs to `http://slack-bot:3000/alert`
5. Bot extracts alert name, severity, summary from payload
6. Claude queries current metrics for context, returns structured diagnosis
7. Bot posts to `#oncall`:
   ```
   🚨 ALERT: [HotROD] HighLatency
   📋 CAUSE: p99 latency crossed 1s threshold on GET_/customer for 2+ minutes
   🔧 FIX: Check downstream database query performance for the customer service
   ```

---

## MCP Tools

Defined in `mcp_server/server.py`:

| Tool | PromQL |
|---|---|
| `get_service_health` | `up{job="hotrod"}` + error rate per endpoint |
| `get_top_errors` | `topk(5, rate(error_count) / rate(total))` |
| `get_latency` | `histogram_quantile(0.50/0.95/0.99, ...)` per endpoint |

---

## Alert rules

Defined in `k8s/monitoring/alert-rules.yaml`:

| Alert | Condition | Severity |
|---|---|---|
| `HighErrorRate` | Error rate > 5% for 1 minute | critical |
| `HighLatency` | p99 > 1s for 2 minutes | warning |
| `ServiceDown` | `up{job="hotrod"} == 0` for 1 minute | critical |

---

## Extensibility — adding a new service

The bot is service-agnostic. To monitor a second service:

1. Deploy a second MCP server pointed at the new service's Prometheus metrics
2. Deploy a second bot instance with `SERVICE_NAME=<your-service>` env var
3. Both bots post to the same `#oncall` channel with their service name labelled

No changes to the Slack bot code or the alert routing logic.

---

## Project structure

```
slack_bot/
  bot.py              # Slack Bolt app + FastAPI + Claude + MCP client
  requirements.txt
  Dockerfile

mcp_server/
  server.py           # FastMCP server with Prometheus tools
  requirements.txt
  Dockerfile

k8s/
  hotrod/             # HotROD deployment + ServiceMonitor
  mcp-server/         # MCP server deployment + ClusterIP service
  slack-bot/          # Slack bot deployment + LoadBalancer service
  monitoring/         # Prometheus values, alert rules, alertmanager config

decisions.md          # Architectural decision log (15 decisions)
limitations.md        # Known limitations
```

---

## Judge access

Sandbox member access has been granted to `slackhack@salesforce.com` and `testing@devpost.com`.

### Testing slash commands (pull-based)

Open the sandbox workspace and type in any channel:
```
/health
/top
```

Claude will respond within 5-10 seconds with a structured summary.

### Testing proactive alerts (push-based)

**Step 1 — Generate traffic to trigger high latency:**
```bash
for i in $(seq 1 50); do
  curl -s "http://136.64.135.195:8080/dispatch?customer=123" > /dev/null &
done
wait
```

**Step 2 — Check `#oncall` in Slack**

A message will appear in the format:
```
🚨 ALERT: [HotROD] HighLatency
📋 CAUSE: p99 latency crossed 1s threshold on GET_/customer
🔧 FIX: Check downstream database query performance
```

> Note: Alert `for` duration is set to `30s` for demo responsiveness. Production recommendation is `2m` to avoid false positives.

---

## Running locally

**Prerequisites:** Docker, `kubectl` pointed at a cluster, Prometheus running, Slack app created at api.slack.com.

```bash
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_SIGNING_SECRET=...
export ANTHROPIC_API_KEY=sk-ant-...
export SERVICE_NAME=HotROD

# Start MCP server
cd mcp_server && pip install -r requirements.txt && python server.py

# Start Slack bot (separate terminal)
cd slack_bot && pip install -r requirements.txt && python bot.py
```

Use [ngrok](https://ngrok.com) to expose the bot to Slack — required locally since Slack needs a public URL:
```bash
ngrok http 3000
```

Set the ngrok URL as your Slack app's slash command request URL at api.slack.com. On GKE, use the LoadBalancer IP instead — no ngrok needed.

---

## Deploying to GKE

```bash
# Build and push images
docker buildx build --platform linux/amd64 -t <registry>/mcp-server:latest --push mcp_server/
docker buildx build --platform linux/amd64 -t <registry>/slack-bot:latest --push slack_bot/

# Create secrets
kubectl create secret generic slack-bot-secrets \
  --from-literal=SLACK_BOT_TOKEN=xoxb-... \
  --from-literal=SLACK_SIGNING_SECRET=... \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-...

# Deploy
kubectl apply -f k8s/hotrod/
kubectl apply -f k8s/mcp-server/
kubectl apply -f k8s/slack-bot/
kubectl apply -f k8s/monitoring/
```


