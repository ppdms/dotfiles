#!/bin/bash
# Test Cloudflare Access authentication with service token

set -e

# Load .env if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

SERVER_URL="${CHISEL_SERVER:-}"
CLIENT_ID="${CF_ACCESS_CLIENT_ID:-}"
CLIENT_SECRET="${CF_ACCESS_CLIENT_SECRET:-}"

# Check for credentials
if [ -z "$SERVER_URL" ] || [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "Error: Cloudflare Access credentials not set"
    echo ""
    echo "Make sure .env file exists with:"
    echo "  CF_ACCESS_CLIENT_ID=your-id.access"
    echo "  CF_ACCESS_CLIENT_SECRET=your-secret"
    echo "  CHISEL_SERVER=https://proxy.example.com"
    exit 1
fi

echo "=== Testing Cloudflare Access Authentication ==="
echo "Server: $SERVER_URL"
echo "Client ID: ${CLIENT_ID:0:20}..."
echo ""

# Test from Mac directly (bypasses Lima VM)
echo "Testing from Mac..."
curl -k -v \
  -H "CF-Access-Client-Id: $CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CLIENT_SECRET" \
  "$SERVER_URL" 2>&1 | grep -E "(HTTP|CF-|< |subject:|issuer:)"

echo ""
echo ""
echo "If you see 'HTTP/2 200' or similar, authentication is working!"
echo "If you see '403 Forbidden', check your service token configuration."
echo "A corporate TLS inspection certificate may be expected on managed networks."
