#!/bin/sh
set -e

echo "=== Sandbox router starting ==="

# IP forwarding is enabled via docker-compose sysctls (net.ipv4.ip_forward=1)

# --- Base iptables rules ---
# Default FORWARD policy: DROP (fail-closed)
iptables -P FORWARD DROP

# Allow established/related connections (return traffic)
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# NAT: masquerade outbound traffic on eth0 (dev-sandbox, the external interface)
iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

# --- Re-apply persisted per-network rules ---
if [ -d /etc/sandbox/rules ] && [ "$(ls -A /etc/sandbox/rules 2>/dev/null)" ]; then
    echo "Re-applying persisted firewall rules..."
    for rulefile in /etc/sandbox/rules/*; do
        SUBNET=$(awk '{print $1}' "$rulefile")
        MODE=$(awk '{print $2}' "$rulefile")
        if [ -n "$SUBNET" ] && [ -n "$MODE" ]; then
            echo "  $SUBNET ($MODE)"
            /scripts/apply-rules.sh "$SUBNET" "$MODE"
        fi
    done
fi

echo "=== Sandbox router ready ==="

# Keep running (accept docker exec calls)
exec sleep infinity
