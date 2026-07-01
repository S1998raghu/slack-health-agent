import os
from slack_bolt.async_app import AsyncApp as App
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler as SlackRequestHandler
from fastapi import FastAPI, Request
import anthropic
from mcp import ClientSession
from mcp.client.sse import sse_client
import uvicorn

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000/sse")
SERVICE_NAME = os.getenv("SERVICE_NAME", "HotROD")

app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
api = FastAPI()
handler = SlackRequestHandler(app)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


async def ask_claude(prompt: str) -> str:
    try:
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                mcp_tools = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.inputSchema,
                    }
                    for tool in tools.tools
                ]
                messages = [{"role": "user", "content": prompt}]

                while True:
                    response = anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=1024,
                        tools=mcp_tools,
                        messages=messages,
                    )

                    if response.stop_reason == "end_turn":
                        for block in response.content:
                            if hasattr(block, "text"):
                                return block.text
                        break

                    tool_uses = [b for b in response.content if b.type == "tool_use"]
                    if not tool_uses:
                        break

                    messages.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for tool_use in tool_uses:
                        result = await session.call_tool(tool_use.name, tool_use.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result.content[0].text,
                        })
                    messages.append({"role": "user", "content": tool_results})

                return "No response generated."
    except Exception as e:
        return f"⚠️ Could not fetch data: {str(e)}"


@app.command("/health")
async def health_command(ack, respond):
    await ack()
    result = await ask_claude(f"""Check the health of the {SERVICE_NAME} service and reply in exactly this format:

🟢/🔴 Status: UP or DOWN
📊 Error rates:
  <endpoint>: <rate>% <emoji>
⚡ Worst latency: <endpoint> p99 <value>s
👀 Watch out: one sentence on the biggest risk right now

Use 🔴 if error rate > 5% or p99 > 1s, 🟡 if > 1% or > 0.5s, 🟢 otherwise. No extra text.""")
    await respond(result)


@app.command("/top")
async def top_command(ack, respond):
    await ack()
    result = await ask_claude(f"""Get the top 5 {SERVICE_NAME} endpoints by error rate and their p99 latency. Reply in exactly this format:

🔥 Top endpoints by error rate:
  1. <endpoint> — <error rate>% errors, p99 <latency>s <emoji>
  2. ...

Use 🔴 if needs immediate attention, 🟡 if worth watching, 🟢 if healthy. No extra text.""")
    await respond(result)


@api.post("/slack/{command}")
async def slack_commands(req: Request):
    return await handler.handle(req)


@api.post("/alert")
async def receive_alert(req: Request):
    body = await req.json()
    alerts = body.get("alerts", [])
    for alert in alerts:
        name = alert["labels"].get("alertname", "unknown")
        severity = alert["labels"].get("severity", "unknown")
        summary = alert["annotations"].get("summary", "")
        prompt = f"""Alert fired on {SERVICE_NAME}: {name} (severity: {severity}). Summary: {summary}.

Reply in exactly this format, no extra text. Do not change the first line:

🚨 ALERT: [{SERVICE_NAME}] {name}
📋 CAUSE: one sentence explaining what metric crossed what threshold and why it matters
🔧 FIX: one sentence on the single most important thing the on-call engineer should do right now"""
        result = await ask_claude(prompt)
        header = f"🚨 ALERT: [{SERVICE_NAME}] {name}\n"
        if not result.startswith(header):
            result = header + "\n".join(result.split("\n")[1:])
        await app.client.chat_postMessage(channel="#oncall", text=result)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=3000)
