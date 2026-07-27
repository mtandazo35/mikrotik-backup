"""Levanta el panel de verdad y le manda lo mismo que le manda el navegador.

Existe por un fallo que se escapo a produccion teniendo 1047 comprobaciones en
verde: un boton nuevo llamaba a validar_equipo() sin uno de sus argumentos
obligatorios. TypeError, 502 en la cara del usuario, y ninguna prueba se entero.

El motivo es que NADA tocaba los manejadores de POST, y el panel entero funciona
por POST: altas, edicion, bajas, cuentas, ajustes, importacion, desbloqueos. Los
demas archivos prueban funciones sueltas (que estan bien probadas) y HTML ya
pintado (idem), pero entre una cosa y la otra queda el manejador, que es donde
se juntan y donde se rompe.

Esto no comprueba que la logica sea correcta: comprueba que cada formulario se
puede ENVIAR sin que el servidor se caiga. Es poco a proposito, porque tiene que
seguir siendo barato de mantener; lo que aporta es que ningun manejador puede
volver a salir con un error de los que se ven a la primera llamada.

Levanta el panel sobre un directorio temporal, en un puerto suelto. No toca red,
ni equipos, ni nada de fuera: donde hace falta un router que no contesta se
apunta a un puerto cerrado del propio 127.0.0.1.

Ejecutar:  python -m tests.test_panel
"""

import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from pathlib import Path

from mkbackup import web
from mkbackup.config import Config
from mkbackup.sesion import Sesiones, hashear

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


class Panel:
    """El panel corriendo contra un directorio temporal."""

    CLAVE = "clave-de-prueba-1234"

    def __init__(self, tmp: Path):
        cfg = Config()
        cfg.inventario = str(tmp / "inventory.csv")
        cfg.almacen.git = str(tmp / "configs.git")
        cfg.almacen.binarios = str(tmp / "binarios")
        cfg.almacen.estado = str(tmp / "estado.json")
        cfg.almacen.usuarios = str(tmp / "usuarios.json")
        cfg.almacen.hechos = str(tmp / "equipos.json")
        cfg.almacen.auditoria = str(tmp / "auditoria.log")
        cfg.almacen.ajustes = str(tmp / "ajustes.json")
        cfg.web.clave_hash = hashear(self.CLAVE)
        # Corto a proposito: hay comprobaciones que apuntan a un puerto cerrado
        # para que la sonda de identidad falle, y no se puede pagar el minuto de
        # rigor en cada una.
        cfg.ssh.timeout = 2

        Path(cfg.inventario).write_text(
            "nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave\n"
            "Equipo-Uno,Empresa Uno,10.20.0.1,22,core,,usr,cla\n",
            encoding="utf-8",
        )

        self.cfg = cfg
        self.ctx = web.Contexto(cfg, Sesiones(
            duracion_horas=cfg.web.sesion_horas,
            intentos_max=cfg.web.intentos_max,
            bloqueo_segundos=cfg.web.bloqueo_segundos,
        ))
        self.ctx.usuarios.sembrar(cfg.web.usuario, cfg.web.clave_hash)

        self.srv = web._Servidor(
            ("127.0.0.1", 0), partial(web.Manejador, ctx=self.ctx)
        )
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.cookie = ""
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _abrir(self, pet):
        """Sin seguir redirecciones, que es lo que hace utiles a las respuestas.

        urlopen sigue el 303 el solito, y entonces pasan dos cosas malas: la
        cookie del login se pierde (Set-Cookie va en el 303, no en la pagina a
        la que lleva) y un formulario que guarda bien queda indistinguible de
        uno que solo pinta. Aqui interesa el codigo tal cual.
        """
        class SinSeguir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_a, **_k):
                return None

        return urllib.request.build_opener(SinSeguir).open(pet, timeout=30)

    def pedir(self, ruta, datos=None):
        """(codigo, cuerpo). No lanza: un 500 es un resultado que hay que ver."""
        cuerpo = urllib.parse.urlencode(datos).encode() if datos is not None else None
        pet = urllib.request.Request(
            self.base + ruta, data=cuerpo, method="POST" if cuerpo else "GET"
        )
        if self.cookie:
            pet.add_header("Cookie", self.cookie)
        try:
            with self._abrir(pet) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # Con el manejador de arriba, una redireccion llega por aqui.
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {exc}"

    def entrar(self) -> bool:
        datos = urllib.parse.urlencode(
            {"usuario": self.cfg.web.usuario, "clave": self.CLAVE}
        ).encode()
        pet = urllib.request.Request(self.base + "/entrar", data=datos)
        try:
            with self._abrir(pet) as r:
                galleta = r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as exc:
            galleta = exc.headers.get("Set-Cookie", "")
        self.cookie = galleta.split(";")[0] if galleta else ""
        return bool(self.cookie)

    def cerrar(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


# Un router que no contesta, sin depender de la red: el puerto 9 de la propia
# maquina esta cerrado, asi que la sonda falla en milisegundos.
EQUIPO_MUDO = {
    "empresa": "Empresa Uno",
    "ip": "127.0.0.1",
    "puerto": "9",
    "grupo": "core",
    "intervalo": "",
    "usuario": "usr",
    "clave": "cla",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        panel = Panel(Path(tmp))
        try:
            comprobar("se entra al panel", panel.entrar())

            print("\n--- Las paginas se sirven ---")
            for ruta in ("/", "/equipos", "/cambios", "/usuarios", "/auditoria",
                         "/ajustes", "/api/estado", "/equipos/nuevo", "/importar",
                         "/cuenta"):
                codigo, _ = panel.pedir(ruta)
                comprobar(f"GET {ruta} -> 200 (dio {codigo})", codigo == 200)

            print("\n--- Preguntarle el nombre al router ---")
            # EL fallo que se escapo, exactamente como llega del navegador.
            codigo, html = panel.pedir(
                "/equipos/editar",
                {**EQUIPO_MUDO, "accion": "identidad",
                 "original": "Equipo-Uno", "nombre": "Equipo-Uno"},
            )
            comprobar(f"al editar no revienta (dio {codigo})", codigo == 200)
            comprobar("devuelve el formulario y no una pagina de error",
                      'name="nombre"' in html)
            comprobar("con lo ya tecleado intacto",
                      'value="Empresa Uno"' in html and 'value="127.0.0.1"' in html)
            comprobar("y explicando que el equipo no contesto",
                      "no dijo como se llama" in html)
            comprobar("sin haber guardado nada",
                      "Equipo-Uno" in Path(panel.cfg.inventario).read_text(
                          encoding="utf-8"))

            codigo, html = panel.pedir(
                "/equipos/nuevo", {**EQUIPO_MUDO, "accion": "identidad", "nombre": ""}
            )
            comprobar(f"al dar de alta tampoco (dio {codigo})", codigo == 200)
            comprobar("y el boton sigue ahi para volver a probar",
                      'value="identidad"' in html)

            codigo, html = panel.pedir(
                "/equipos/nuevo",
                {**EQUIPO_MUDO, "ip": "no es una ip", "accion": "identidad",
                 "nombre": ""},
            )
            comprobar(f"con la IP mal escrita contesta sin caerse (dio {codigo})",
                      codigo == 200)

            print("\n--- Todos los formularios se pueden enviar ---")
            # No se comprueba que hagan lo correcto: para eso estan los otros
            # archivos. Se comprueba que se pueden llamar. 200 es "formulario
            # con errores", 303 "guardado", 4xx "no puedes o no vale"; 500 y 0
            # son que el manejador se cayo.
            envios = [
                ("/equipos/nuevo", {**EQUIPO_MUDO, "nombre": "Equipo-Dos",
                                    "ip": "10.20.0.2", "puerto": "22"}),
                ("/equipos/editar", {**EQUIPO_MUDO, "original": "Equipo-Uno",
                                     "nombre": "Equipo-Uno", "ip": "10.20.0.1",
                                     "puerto": "22"}),
                ("/equipos/baja", {"nombre": "Equipo-Dos"}),
                ("/auditoria/desbloquear", {"ip": "203.0.113.7"}),
                ("/usuarios/nuevo", {"nombre": "operario",
                                     "clave": "clave-larga-1",
                                     "clave2": "clave-larga-1",
                                     "rol": "operador"}),
                ("/ajustes", {"intervalo_minutos": "240"}),
                ("/cuenta", {"actual": Panel.CLAVE,
                             "clave": "otra-clave-larga",
                             "clave2": "otra-clave-larga"}),
                ("/salir", {}),
            ]
            for ruta, datos in envios:
                codigo, cuerpo = panel.pedir(ruta, datos)
                bien = codigo in (200, 303, 400, 401, 403, 404, 409, 429)
                comprobar(f"POST {ruta} -> respuesta sensata (dio {codigo})", bien)
                if not bien:
                    print("        ->", cuerpo[:200].replace("\n", " "))

            print("\n--- Y lo que no existe no tumba nada ---")
            for ruta in ("/no-existe", "/equipos/../../etc/passwd", "/equipos/"):
                codigo, _ = panel.pedir(ruta)
                comprobar(f"GET {ruta} -> 200/303/404 (dio {codigo})",
                          codigo in (200, 303, 404))
            codigo, _ = panel.pedir("/equipos/nuevo", {"nombre": ""})
            comprobar(f"un POST sin casi campos no revienta (dio {codigo})",
                      codigo in (200, 303, 400, 403))
        finally:
            panel.cerrar()

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print("  -", f)
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
