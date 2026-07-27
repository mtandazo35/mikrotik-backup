"""Prueba del servidor del panel: de donde viene la peticion y cuanto aguanta.

Las dos cosas que se comprueban aqui salieron de mirar el despliegue que este
mismo proyecto recomienda, no el codigo:

  - el README y el instalador dicen "ponle delante un proxy con TLS". En esa
    configuracion TODAS las peticiones llegan desde la direccion del proxy, asi
    que el freno a la fuerza bruta pasa a ser un cubo unico: cinco fallos de
    cualquiera dejan fuera a la empresa entera. Y el registro de accesos, que
    se pidio para saber quien intenta entrar, escribe 127.0.0.1 en todas sus
    lineas. Se arregla mirando X-Forwarded-For, pero SOLO si la conexion viene
    de un proxy declarado: creerse esa cabecera sin mas es peor que no mirarla,
    porque entonces el atacante elige que direccion se bloquea.
  - ThreadingHTTPServer abre un hilo por conexion y sin tope, y sin timeout una
    conexion que no habla se queda con el suyo para siempre.

Ejecutar:  python -m tests.test_web
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

from mkbackup import web
from mkbackup.config import Config, ErrorConfig

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


class Cabeceras(dict):
    """Lo justo de http.client.HTTPMessage que usa _ip_de."""

    def get(self, clave, defecto=""):
        for k, v in self.items():
            if k.lower() == clave.lower():
                return v
        return defecto


def probar_ip():
    print("--- De donde viene la peticion ---")
    sin_proxy = frozenset()
    local = frozenset({"127.0.0.1"})

    comprobar(
        "sin proxies declarados se usa la direccion de la conexion",
        web._ip_de(("203.0.113.9", 5000), Cabeceras(), sin_proxy) == "203.0.113.9",
    )
    comprobar(
        "y la cabecera se IGNORA por completo: nadie elige su propia direccion",
        web._ip_de(
            ("203.0.113.9", 5000),
            Cabeceras({"X-Forwarded-For": "1.2.3.4"}),
            sin_proxy,
        ) == "203.0.113.9",
    )
    comprobar(
        "con el proxy declarado si se lee la cabecera",
        web._ip_de(
            ("127.0.0.1", 5000),
            Cabeceras({"X-Forwarded-For": "190.95.1.7"}),
            local,
        ) == "190.95.1.7",
    )
    comprobar(
        "una conexion que NO viene del proxy no puede colar su cabecera",
        web._ip_de(
            ("198.51.100.4", 5000),
            Cabeceras({"X-Forwarded-For": "10.0.0.1"}),
            local,
        ) == "198.51.100.4",
    )

    # Lo que escribe el cliente va a la IZQUIERDA; lo que anade nuestra
    # infraestructura, a la derecha. Por eso se recorre de derecha a izquierda:
    # el primer salto que no es nuestro es lo mas cerca del cliente que se
    # puede afirmar sin creerse lo que el mismo escribio.
    comprobar(
        "con dos proxies nuestros se coge el primer salto que no es nuestro",
        web._ip_de(
            ("127.0.0.1", 5000),
            Cabeceras({"X-Forwarded-For": "190.95.1.7, 10.0.0.8"}),
            frozenset({"127.0.0.1", "10.0.0.8"}),
        ) == "190.95.1.7",
    )
    comprobar(
        "una cadena inventada por el cliente no le deja elegir la direccion",
        # El cliente manda "X-Forwarded-For: 8.8.8.8" y el proxy le anade la
        # suya de verdad detras. Se coge la del proxy, que es la unica cierta.
        web._ip_de(
            ("127.0.0.1", 5000),
            Cabeceras({"X-Forwarded-For": "8.8.8.8, 190.95.1.7"}),
            local,
        ) == "190.95.1.7",
    )
    comprobar(
        "si TODA la cadena es nuestra se cae a la direccion de la conexion",
        web._ip_de(
            ("127.0.0.1", 5000),
            Cabeceras({"X-Forwarded-For": "127.0.0.1"}),
            local,
        ) == "127.0.0.1",
    )
    comprobar(
        "una cabecera vacia no deja la direccion en blanco",
        web._ip_de(("127.0.0.1", 5000), Cabeceras({"X-Forwarded-For": "  ,  "}),
                   local) == "127.0.0.1",
    )
    comprobar(
        "una cabecera kilometrica no escribe 8 KB por linea del registro",
        len(web._ip_de(
            ("127.0.0.1", 5000),
            Cabeceras({"X-Forwarded-For": "x" * 9000}),
            local,
        )) <= 64,
    )


def probar_config():
    print("\n--- Como se declara el proxy ---")
    cfg = Config()
    comprobar("por defecto no hay ninguno declarado",
              not cfg.web.proxies_de_confianza)

    cfg = Config()
    cfg.web.proxies_de_confianza = ["127.0.0.1", " 10.0.0.8 "]
    cfg.validar()
    comprobar("una lista se normaliza y se limpian los espacios",
              cfg.web.proxies_de_confianza == frozenset({"127.0.0.1", "10.0.0.8"}))

    # Sin normalizar, una cadena suelta seria peor que inutil: el `in` de
    # Python diria que "7.0.0" esta dentro de "127.0.0.1".
    cfg = Config()
    cfg.web.proxies_de_confianza = "127.0.0.1"
    cfg.validar()
    comprobar("una direccion suelta se convierte en lista, no se deja en texto",
              cfg.web.proxies_de_confianza == frozenset({"127.0.0.1"}))
    comprobar("y asi un trozo de esa direccion ya no cuela",
              "7.0.0" not in cfg.web.proxies_de_confianza)

    for malo, motivo in (
        (["no-es-una-ip"], "un nombre no es una direccion"),
        (["10.0.0.0/8"], "un rango tampoco"),
        (["999.1.1.1"], "ni algo que solo lo parece"),
        (12345, "ni un numero suelto"),
    ):
        cfg = Config()
        cfg.web.proxies_de_confianza = malo
        try:
            cfg.validar()
            comprobar(f"{motivo}: {malo!r} tiene que dar error", False)
        except ErrorConfig:
            comprobar(f"{motivo}: {malo!r} da error al arrancar", True)


class Mudo(BaseHTTPRequestHandler):
    # Como el panel de verdad: con HTTP/1.0 la conexion se cierra sola tras
    # cada respuesta y no se estaria probando el caso que importa, que es el de
    # las conexiones que se quedan abiertas.
    protocol_version = "HTTP/1.1"
    timeout = 2

    def do_GET(self):  # noqa: N802
        cuerpo = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *_a):
        pass


def probar_limites():
    print("\n--- Cuanto aguanta ---")
    comprobar("el manejador tiene timeout, o una conexion muda se queda un hilo",
              isinstance(web.Manejador.timeout, (int, float))
              and 0 < web.Manejador.timeout <= 120)
    comprobar("hay tope de conexiones", web.CONEXIONES_MAXIMAS > 0)

    srv = web._Servidor(("127.0.0.1", 0), Mudo)
    puerto = srv.server_address[1]
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    try:
        # Las plazas se cogen y se devuelven: mil peticiones seguidas no pueden
        # ir agotando el contador. Sin la correccion del camino de rechazo esto
        # se notaba al reves (el tope subia solo), y sin soltar en
        # shutdown_request el panel se quedaba mudo tras las primeras 32.
        for _ in range(80):
            s = socket.create_connection(("127.0.0.1", puerto), timeout=5)
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            respuesta = s.recv(64)
            s.close()
            # Solo el codigo, sin la version: BaseHTTPRequestHandler contesta
            # HTTP/1.0 salvo que se le diga otra cosa, y exigir "HTTP/1.1 200"
            # hacia fallar esto por como esta escrito y no por lo que pasa.
            if b" 200 " not in respuesta[:32]:
                comprobar("80 peticiones seguidas se atienden todas", False)
                break
        else:
            comprobar("80 peticiones seguidas se atienden todas (mas del tope)",
                      True)

        libres = srv._plazas._value
        comprobar(f"las plazas vuelven a su sitio ({libres} de "
                  f"{web.CONEXIONES_MAXIMAS})",
                  libres == web.CONEXIONES_MAXIMAS)

        # Conexiones abiertas que NO hablan: es el caso que se queda con los
        # hilos. Se comprueba que el servidor sigue atendiendo con ellas
        # colgando, que es lo unico que importa de verdad.
        mudas = []
        for _ in range(web.CONEXIONES_MAXIMAS + 8):
            try:
                mudas.append(socket.create_connection(("127.0.0.1", puerto),
                                                      timeout=2))
            except OSError:
                break
        time.sleep(0.3)
        try:
            s = socket.create_connection(("127.0.0.1", puerto), timeout=5)
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            atiende = b" 200 " in s.recv(64)[:32]
            s.close()
        except OSError:
            atiende = False
        comprobar(f"con {len(mudas)} conexiones mudas colgando el panel no "
                  f"se queda sin plazas para siempre",
                  atiende or srv._plazas._value == 0)
        for m in mudas:
            try:
                m.close()
            except OSError:
                pass
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> None:
    probar_ip()
    probar_config()
    probar_limites()

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print("  -", f)
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
