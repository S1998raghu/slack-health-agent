# Limitations

## 1. Response Latency
Claude takes 5-10s to respond, causing Slack's 3s timeout. Fixed with ack() but UX is still slow.

## 2. No Persistent Memory
Each slash command is a fresh context — Claude has no awareness of previous queries in the same incident.

## 3. Pull-Based Slash Commands
User must ask explicitly. No continuous monitoring — Alertmanager handles push alerts separately.

## 4. ngrok Dependency (Local Dev)
Containerizing moved the app to K8s but Docker Desktop's LoadBalancer gives localhost, not a public IP. ngrok is still needed for Slack to reach the bot locally.

## 5. No Retry Logic
If MCP server or Prometheus is down, the bot returns a friendly error message but does not retry.

## 7. Alert `for` Duration Reduced for Demo
Production best practice is `for: 1m` (HighErrorRate) and `for: 2m` (HighLatency) to avoid false positives from transient spikes. Both are set to `for: 30s` in this deployment to make alerts fire faster during the demo. Change these back before using in production.

## 6. NaN Metrics
Endpoints with no traffic return NaN from Prometheus. Handled by skipping in MCP tools but Claude still has incomplete data.
