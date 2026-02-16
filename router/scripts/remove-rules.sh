#!/bin/sh
# remove-rules.sh <subnet>
# Removes all FORWARD rules matching a source subnet.
set -e

SUBNET="$1"

if [ -z "$SUBNET" ]; then
    echo "Usage: remove-rules.sh <subnet>" >&2
    exit 1
fi

# Remove all FORWARD rules matching this source subnet
while iptables -D FORWARD -s "$SUBNET" -d 10.0.0.0/8 -j DROP 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -d 172.16.0.0/12 -j DROP 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -d 192.168.0.0/16 -j DROP 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -d 169.254.0.0/16 -j DROP 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -p icmp -j ACCEPT 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -p tcp --dport 80 -j ACCEPT 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -p tcp --dport 443 -j ACCEPT 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -p udp --dport 53 -j ACCEPT 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -p tcp --dport 53 -j ACCEPT 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -j DROP 2>/dev/null; do :; done
while iptables -D FORWARD -s "$SUBNET" -j ACCEPT 2>/dev/null; do :; done

# Remove persisted config
RULEFILE="/etc/sandbox/rules/$(echo "$SUBNET" | tr '/' '_')"
rm -f "$RULEFILE"

echo "Removed rules for $SUBNET"
