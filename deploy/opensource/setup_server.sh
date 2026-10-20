#!/bin/bash
# Prepara un servidor Ubuntu 24.04 LTS recién instalado para la opción A (2 vCPU).
# Ejecutar UNA vez como root:   sudo bash setup_server.sh
#
# Hace: actualiza el sistema, instala Docker + Compose, git, python3, openssl; activa parches automáticos,
# firewall (22/80/443), fail2ban para SSH, un archivo de intercambio de 2 GB y ajustes del kernel para Valkey.
# No toca la configuración de SSH: hágalo usted (ver docs/despliegue-opcion-a-2vcpu.md, paso 1).
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "Ejecute como root: sudo bash setup_server.sh" >&2
    exit 1
fi
. /etc/os-release
if [ "${ID}" != "ubuntu" ]; then
    echo "Este script es para Ubuntu 24.04. En Debian instale Docker desde https://docs.docker.com/engine/install/debian/" >&2
    exit 1
fi

echo "== 1/6 Paquetes"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get install -y docker.io docker-compose-v2 git python3 openssl ufw fail2ban unattended-upgrades curl dnsutils
systemctl enable --now docker

echo "== 2/6 Parches de seguridad automáticos"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== 3/6 Firewall (Docker solo publica 80/443 de Caddy; el resto queda en la red interna)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "== 4/6 fail2ban (bloquea intentos de fuerza bruta por SSH)"
systemctl enable --now fail2ban

echo "== 5/6 Memoria de intercambio (2 GB) para absorber picos sin que el kernel mate procesos"
if ! swapon --show | grep -q .; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
    echo "   ya hay swap configurada, se conserva"
fi

echo "== 6/6 Ajustes del kernel"
cat > /etc/sysctl.d/90-capacidadbus.conf <<'CONF'
# Usar la swap solo como último recurso (las bases de datos sufren si se paginan).
vm.swappiness = 10
# Recomendado por Valkey/Redis: evita fallos al guardar la persistencia en segundo plano.
vm.overcommit_memory = 1
# Más conexiones pendientes para picos de WebSocket.
net.core.somaxconn = 1024
CONF
sysctl --system > /dev/null

if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    usermod -aG docker "${SUDO_USER}"
    echo "El usuario ${SUDO_USER} quedó en el grupo docker (cierre sesión y vuelva a entrar)."
fi

echo
echo "Listo. Siguiente: python3 ops.py check"
