"""Simulador de un MikroTik por SSH, para probar sin hardware.

Levanta un servidor SSH que responde a los comandos que usa mkbackup con
salidas realistas de RouterOS, incluido el ruido que cambia en cada ejecucion
(la fecha, los comentarios intermitentes).

No sustituye una prueba contra un equipo real: aqui no se valida el dialogo
SFTP ni las rarezas de cada version de RouterOS. Sirve para verificar que el
flujo completo funciona de punta a punta.

Uso:
    python -m tests.simulador_routeros [puerto]
"""

from __future__ import annotations

import socket
import sys
import threading
from datetime import datetime
from pathlib import Path

import paramiko

USUARIO = "Respaldo"
PASSWORD = "prueba123"

# El export cambia entre llamadas solo en las lineas de ruido: sirve para
# comprobar que mkbackup NO lo interpreta como un cambio de configuracion.
PLANTILLA_EXPORT = """# {fecha} by RouterOS 7.14.2
# software id = ABCD-1234
#
# model = RB4011iGS+
/interface bridge
add admin-mac=48:8F:5A:00:00:01 auto-mac=no comment=LAN name=bridge1
/interface list
add name=WAN
add name=LAN
/ip pool
add name=dhcp_pool0 ranges=192.168.88.10-192.168.88.254
/ip address
add address=192.168.88.1/24 comment=defconf interface=bridge1 \\
    network=192.168.88.0
/ip dhcp-server
add address-pool=dhcp_pool0 interface=bridge1 name=defconf
/ip dns
set allow-remote-requests=yes servers={dns}
/ppp secret
add name=cliente001 password=secreto123 service=pppoe
/system identity
set name=MikroTik-Simulado
/system clock
set time-zone-name=America/Guayaquil
{ruido}"""

RESOURCE = """                   uptime: 3w4d10h30m
                  version: 7.14.2 (stable)
               build-time: Mar/15/2026 09:22:11
              free-memory: 890.5MiB
             total-memory: 1024.0MiB
                      cpu: ARM64
                cpu-count: 4
            architecture-name: arm64
                   board-name: RB4011iGS+
                     platform: MikroTik
"""

# Estado mutable para simular cambios de configuracion entre ejecuciones.
# El DNS se puede cambiar en caliente escribiendo en ARCHIVO_DNS: asi se prueba
# que un cambio REAL si genera commit, sin reiniciar el simulador.
ESTADO = {"contador": 0}
ARCHIVO_DNS = Path(__file__).with_name(".dns_simulado")
DNS_POR_DEFECTO = "8.8.8.8,8.8.4.4"


def dns_actual() -> str:
    if ARCHIVO_DNS.is_file():
        valor = ARCHIVO_DNS.read_text(encoding="utf-8").strip()
        if valor:
            return valor
    return DNS_POR_DEFECTO


def salida_para(comando: str) -> str:
    comando = comando.strip()

    if comando.startswith("/system resource print"):
        return RESOURCE

    if comando.startswith("/export"):
        ESTADO["contador"] += 1
        # Ruido distinto en cada llamada: si mkbackup no lo filtra, cada
        # respaldo pareceria un cambio.
        ruido = [
            "# inactive time",
            "# poe-out status: short_circuit",
            f"# ether{ESTADO['contador'] % 5 + 1} not ready",
        ]
        return PLANTILLA_EXPORT.format(
            fecha=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            dns=dns_actual(),
            ruido="\n".join(ruido) + "\n",
        )

    if comando.startswith("/system backup save"):
        return "Configuration backup saved\n"

    return ""


class Servidor(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        if username == USUARIO and password == PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        texto = command.decode("utf-8", errors="replace")
        threading.Thread(
            target=self._responder, args=(channel, texto), daemon=True
        ).start()
        return True

    @staticmethod
    def _responder(channel, comando: str) -> None:
        try:
            # RouterOS entrega CRLF: importante para probar la normalizacion.
            salida = salida_para(comando).replace("\n", "\r\n")
            channel.sendall(salida.encode("utf-8"))
            channel.send_exit_status(0)
        finally:
            channel.close()


def atender(cliente: socket.socket, host_key: paramiko.PKey) -> None:
    transporte = paramiko.Transport(cliente)
    try:
        transporte.add_server_key(host_key)
        transporte.start_server(server=Servidor())
        canal = transporte.accept(20)
        if canal is not None:
            canal.join(20) if hasattr(canal, "join") else None
            # El hilo de _responder cierra el canal al terminar
            import time
            for _ in range(200):
                if canal.closed:
                    break
                time.sleep(0.05)
    except (paramiko.SSHException, EOFError, OSError):
        pass
    finally:
        try:
            transporte.close()
        except Exception:  # noqa: BLE001
            pass


def main(puerto: int = 2222) -> None:
    host_key = paramiko.RSAKey.generate(2048)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # A proposito SIN SO_REUSEADDR: en Windows permite que dos procesos bindeen
    # el mismo puerto y el reparto de conexiones queda indefinido. Un simulador
    # viejo quedandose con el trafico produce pruebas que "pasan" contra la
    # version equivocada del codigo. Mejor fallar aqui, ruidosamente.
    try:
        sock.bind(("127.0.0.1", puerto))
    except OSError as exc:
        print(f"ERROR: no se pudo abrir el puerto {puerto}: {exc}")
        print("Puede haber otro simulador corriendo. En Windows:")
        print(f"  Get-NetTCPConnection -LocalPort {puerto} -State Listen")
        raise SystemExit(1) from exc
    sock.listen(20)

    print(f"Simulador RouterOS escuchando en 127.0.0.1:{puerto}")
    print(f"  usuario:  {USUARIO}")
    print(f"  password: {PASSWORD}")
    print("Ctrl+C para parar.\n", flush=True)

    try:
        while True:
            cliente, origen = sock.accept()
            print(f"  conexion desde {origen[0]}:{origen[1]}", flush=True)
            threading.Thread(target=atender, args=(cliente, host_key), daemon=True).start()
    except KeyboardInterrupt:
        print("\nSimulador detenido.")
    finally:
        sock.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2222)
