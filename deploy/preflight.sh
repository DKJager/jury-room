#!/bin/bash
# Computed preflight (#2139, #2467): every line derives from an exit code; nothing is echoed as a fixed pass.
IFACE="${IFACE:-$(ip route show default | awk '{print $5; exit}')}"; echo "public interface: $IFACE"
fail=0
systemctl is-active --quiet docker && echo "docker: active" || { echo "docker: NOT active"; fail=1; }
free_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9); echo "disk free: ${free_gb} GB"; [ "$free_gb" -gt 20 ] || fail=1
iptables -C DOCKER-USER -i "$IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN 2>/dev/null || iptables -I DOCKER-USER 1 -i "$IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
iptables -C DOCKER-USER -i "$IFACE" -j DROP 2>/dev/null || iptables -A DOCKER-USER -i "$IFACE" -j DROP
iptables -C DOCKER-USER -i "$IFACE" -j DROP 2>/dev/null && echo "DOCKER-USER drop rule: present" || { echo "DOCKER-USER drop rule: MISSING"; fail=1; }
read l1 _ < /proc/loadavg; echo "load1: $l1"
exit $fail
