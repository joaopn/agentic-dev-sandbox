#!/bin/sh
# apply-rules.sh <subnet> <locked|open>
# Idempotent: removes existing rules for this subnet before applying.
set -e

SUBNET="$1"
MODE="$2"

if [ -z "$SUBNET" ] || [ -z "$MODE" ]; then
    echo "Usage: apply-rules.sh <subnet> <locked|open>" >&2
    exit 1
fi

# Remove existing rules for this subnet (idempotent)
/scripts/remove-rules.sh "$SUBNET" 2>/dev/null || true

# Block RFC1918 destinations
iptables -A FORWARD -s "$SUBNET" -d 10.0.0.0/8 -j DROP
iptables -A FORWARD -s "$SUBNET" -d 172.16.0.0/12 -j DROP
iptables -A FORWARD -s "$SUBNET" -d 192.168.0.0/16 -j DROP
iptables -A FORWARD -s "$SUBNET" -d 169.254.0.0/16 -j DROP

if [ "$MODE" = "locked" ]; then
    # Allow only ICMP, HTTP, HTTPS, DNS
    iptables -A FORWARD -s "$SUBNET" -p icmp -j ACCEPT
    iptables -A FORWARD -s "$SUBNET" -p tcp --dport 80 -j ACCEPT
    iptables -A FORWARD -s "$SUBNET" -p tcp --dport 443 -j ACCEPT
    iptables -A FORWARD -s "$SUBNET" -p udp --dport 53 -j ACCEPT
    iptables -A FORWARD -s "$SUBNET" -p tcp --dport 53 -j ACCEPT
    iptables -A FORWARD -s "$SUBNET" -j DROP
fi
# open mode: only RFC1918 blocked, all other traffic falls through to default
# (which is DROP, but established/related is ACCEPTed, and new outbound needs
#  an explicit ACCEPT)

if [ "$MODE" = "open" ]; then
    iptables -A FORWARD -s "$SUBNET" -j ACCEPT
fi

# Persist config for re-application on restart
RULEFILE="/etc/sandbox/rules/$(echo "$SUBNET" | tr '/' '_')"
echo "$SUBNET $MODE" > "$RULEFILE"

echo "Applied $MODE rules for $SUBNET"
