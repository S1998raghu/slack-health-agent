import os
import httpx 
from mcp.server.fastmcp import FastMCP

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
mcp = FastMCP("prometheus-health", host="0.0.0.0", port=8000)

@mcp.tool()
async def get_service_health() -> str:
    """Get up/down status and error rate for all HotROD endpoints."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params = {
                "query": 'up{job="hotrod"}'
            },
            timeout = 10,
        )
        resp.raise_for_status()
        results = resp.json()["data"]["result"]
        service_up = any(r["value"][1] == "1" for r in results)

        error_resp = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={
                "query": (
                    "sum by (exported_endpoint) ("
                    "  rate(hotrod_request_latency_count{error='true'}[5m])"
                    ") / sum by (exported_endpoint) ("
                    "  rate(hotrod_request_latency_count[5m])"
                    ")"
                )
            },
            timeout=10,
        )
        error_resp.raise_for_status()
        error_results = error_resp.json()["data"]["result"]
        lines = [f"HotROD service: {'UP' if service_up else 'DOWN'}"]
        for r in error_results:
            endpoint = r["metric"].get("exported_endpoint", "unknown")
            rate = float(r["value"][1])
            status = "🔴" if rate > 0.05 else "🟡" if rate > 0.01 else "🟢"
            lines.append(f"  {status} {endpoint}: {rate:.1%}")
        return "\n".join(lines)

@mcp.tool()
async def get_top_errors() -> str:
    """Get top 5 HotROD endpoints by error rate right now."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={
                "query": (
                    "topk(5, sum by (exported_endpoint) ("
                    "  rate(hotrod_request_latency_count{error='true'}[5m])"
                    ") / sum by (exported_endpoint) ("
                    "  rate(hotrod_request_latency_count[5m])"
                    "))"
                )
            },
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()["data"]["result"]

    if not results:
        return "No errors detected in the last 5 minutes."

    lines = ["Top 5 endpoints by error rate (last 5m):"]
    i = 1
    for r in results:
        endpoint = r["metric"].get("exported_endpoint", "unknown")
        val = r["value"][1]
        if val == "NaN":
            continue
        rate = float(val)
        lines.append(f"  {i}. {endpoint}: {rate:.1%} error rate")
        i += 1
    if len(lines) == 1:
        return "No errors detected in the last 5 minutes."
    return "\n".join(lines)
@mcp.tool()
async def get_latency() -> str:
    """Get p50/p95/p99 latency for all HotROD endpoints."""
    async with httpx.AsyncClient() as client:
        lines = ["Latency percentiles (last 5m):"]
        for quantile, label in [("0.50", "p50"), ("0.95", "p95"), ("0.99", "p99")]:
            resp = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={
                    "query": (
                        f"histogram_quantile({quantile}, "
                        f"sum by (exported_endpoint, le) ("
                        f"  rate(hotrod_request_latency_bucket{{error='false'}}[5m])"
                        f"))"
                    )
                },
                timeout=10,
            )
            resp.raise_for_status()
            for r in resp.json()["data"]["result"]:
                ep = r["metric"].get("exported_endpoint", "unknown")
                val = r["value"][1]
                if val == "NaN":
                    continue
                val = float(val)
                status = "🔴" if val > 1.0 else "🟡" if val > 0.5 else "🟢"
                lines.append(f"  {status} {ep} {label}: {val:.3f}s")
    return "\n".join(lines)

# server.py
if __name__ == "__main__":
    mcp.run(transport="sse")

