import asyncio
import os
import httpx
import json

async def test_mcp_handshake():
    base_url = os.environ.get("MCP_SERVER_URL", "http://localhost:8080")
    print(f"Connecting to MCP Server at: {base_url}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Connect to SSE
        print("1. Connecting to SSE...")
        async with client.stream("GET", f"{base_url}/sse") as response:
            print(f"SSE Status: {response.status_code}")
            endpoint_url = None
            async for line in response.aiter_lines():
                print(f"SSE Line: {line}")
                if line.startswith("data: "):
                    endpoint_url = line[6:].strip()
                    break
            
            print(f"2. Got message endpoint: {endpoint_url}")
            if not endpoint_url:
                print("Failed to get endpoint URL")
                return

            if not endpoint_url.startswith("http"):
                full_post_url = f"{base_url}{endpoint_url}"
            else:
                full_post_url = endpoint_url

            print(f"3. Sending JSON-RPC initialize to {full_post_url}...")
            init_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"}
                }
            }
            post_res = await client.post(full_post_url, json=init_payload)
            print(f"Initialize POST status: {post_res.status_code}")

            # Read response from SSE
            print("4. Reading initialize response from SSE...")
            async for line in response.aiter_lines():
                print(f"SSE Line (Init Response): {line}")
                if line.startswith("data: ") and "result" in line:
                    break

            # Send initialized notification
            print("5. Sending notifications/initialized...")
            notif_payload = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }
            await client.post(full_post_url, json=notif_payload)

            # Send tools/list
            print("6. Sending tools/list...")
            tools_payload = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {}
            }
            tools_res = await client.post(full_post_url, json=tools_payload)
            print(f"tools/list POST status: {tools_res.status_code}")

            # Read tools list from SSE
            print("7. Reading tools/list response from SSE...")
            async for line in response.aiter_lines():
                print(f"SSE Line (Tools List): {line}")
                if line.startswith("data: ") and "tools" in line:
                    print("\n🎉 SUCCESS! Received tools list from MCP Server!")
                    break

if __name__ == "__main__":
    asyncio.run(test_mcp_handshake())
