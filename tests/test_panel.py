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
import time
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
                ("/ajustes/respaldar", {}),
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

            print("\n--- Respaldar ahora ---")
            # El panel NO lanza el respaldo: deja una peticion en disco que
            # recoge el programador. Si lo lanzara el mismo habria dos procesos
            # escribiendo en el mismo repositorio de git, que es lo unico que
            # este proyecto no se puede permitir.
            from mkbackup import planificador as pl

            pl.ruta_peticion(panel.cfg).unlink(missing_ok=True)
            codigo, html = panel.pedir("/ajustes/respaldar", {"x": "1"})
            comprobar(f"el boton contesta (dio {codigo})", codigo == 200)
            comprobar("y deja la peticion en disco, para el programador",
                      pl.ruta_peticion(panel.cfg).is_file())
            comprobar("con el nombre de quien lo pidio",
                      pl.recoger_peticion(panel.cfg) == panel.cfg.web.usuario)
            comprobar("y el panel dice que quedo pedido",
                      "pedido" in html.lower())

            print("\n--- Preguntar el nombre a toda la flota ---")
            from mkbackup import identidades

            codigo, html = panel.pedir("/ajustes")
            comprobar("la pantalla ofrece el boton masivo",
                      'action="/ajustes/identidades"' in html)
            comprobar("y avisa de lo que hace 'todos'",
                      "se pierde" in html)

            identidades.olvidar()
            # Sin equipos provisionales no hay a quien preguntar, y eso se dice
            # en vez de arrancar un sondeo vacio que parece que hizo algo.
            codigo, html = panel.pedir(
                "/ajustes/identidades", {"alcance": "provisionales"}
            )
            comprobar(f"sin candidatos contesta sin arrancar nada (dio {codigo})",
                      codigo == 200 and "No hay ningun equipo" in html)
            comprobar("y no dejo ningun sondeo", identidades.ultimo() is None)

            # Un alcance inventado desde fuera no puede convertirse en "todos":
            # eso renombraria la flota entera de quien solo pedia los sin nombre.
            codigo, html = panel.pedir(
                "/ajustes/identidades", {"alcance": "; rm -rf /"}
            )
            comprobar(f"un alcance inventado cae en el prudente (dio {codigo})",
                      codigo == 200 and "No hay ningun equipo" in html)

            codigo, html = panel.pedir("/ajustes/identidades", {"alcance": "todos"})
            comprobar(f"con 'todos' arranca (dio {codigo})", codigo == 200)
            comprobar("y lo dice", "Preguntando a 1" in html)
            sondeo = identidades.ultimo()
            comprobar("queda un sondeo registrado", sondeo is not None)
            comprobar("a nombre de quien le dio",
                      sondeo is not None and sondeo.quien == panel.cfg.web.usuario)

            codigo, cuerpo = panel.pedir("/api/identidades")
            comprobar(f"el JSON del avance se sirve (dio {codigo})", codigo == 200)
            comprobar("y trae las cuentas", '"total": 1' in cuerpo.replace("\n", ""))

            # El equipo apunta a una IP que no contesta: el sondeo acaba solo.
            limite = time.monotonic() + 30
            while sondeo is not None and sondeo.corriendo and time.monotonic() < limite:
                time.sleep(0.05)
            comprobar("termina solo, sin dejar el hilo colgado",
                      sondeo is not None and not sondeo.corriendo)
            comprobar("y el equipo que no contesto se queda como estaba",
                      "Equipo-Uno" in Path(panel.cfg.inventario).read_text(
                          encoding="utf-8"))

            # Mientras el programador esta en un ciclo NO se puede lanzar: son
            # dos procesos escribiendo el mismo inventario y el mismo repo.
            identidades.olvidar()
            import json as _json

            marca = pl.ruta_programador(panel.cfg)
            marca.write_text(_json.dumps({"corriendo": True}), encoding="utf-8")
            codigo, html = panel.pedir("/ajustes/identidades", {"alcance": "todos"})
            comprobar(f"con un respaldo en marcha se niega (dio {codigo})",
                      codigo == 409)
            comprobar("explicando por que", "respaldo en marcha" in html)
            comprobar("y sin arrancar nada", identidades.ultimo() is None)
            marca.unlink(missing_ok=True)

            print("\n--- La hoja de estilos se sirve aparte y se cachea ---")
            # Era el 89% de cada pagina (28 KB de 31) y viajaba entera en cada
            # clic del paginador, con no-store, asi que el navegador no podia
            # reutilizarla nunca. Aparte y cacheada, cambiar de pagina pasa de
            # 31 KB a 3.
            from mkbackup import paginas as _pg

            pet = urllib.request.Request(panel.base + _pg.RUTA_ESTILO)
            with panel._abrir(pet) as r:
                css = r.read().decode("utf-8")
                cabeceras = {k.lower(): v for k, v in r.headers.items()}
            comprobar(f"la hoja se sirve ({len(css) // 1024} KB)",
                      len(css) > 1000 and "--fondo" in css)
            comprobar("con el tipo correcto",
                      "text/css" in cabeceras.get("content-type", ""))
            # Sin esto no se gana nada: seguiria bajandose en cada pagina.
            cache = cabeceras.get("cache-control", "")
            comprobar(f"y se puede cachear ({cache})",
                      "max-age=" in cache and "no-store" not in cache)

            # Se sirve SIN sesion a proposito: es el estilo de la propia
            # pantalla de entrada, donde todavia no hay sesion que valga.
            sin_galleta = urllib.request.Request(panel.base + "/estilo.css")
            with panel._abrir(sin_galleta) as r:
                comprobar("y sin pedir login, que es el estilo del propio login",
                          r.status == 200)

            codigo, html = panel.pedir("/cambios")
            comprobar(f"y las paginas ya no la llevan dentro "
                      f"({len(html) // 1024} KB)",
                      "<style" not in html and len(html) < 12 * 1024)

            print("\n--- Estado cuenta la flota de AHORA, no la del ultimo ciclo ---")
            # El archivo de estado es la foto del ultimo ciclo. Entre ese ciclo
            # y ahora la flota cambia, y el panel enseñaba la foto: se daba de
            # baja un equipo y Estado seguia contandolo (y pintandolo en la
            # tabla y en las tartas) durante horas, hasta el siguiente ciclo.
            # Con un solo equipo, el panel decia "1 equipo, 1 respaldado" de un
            # router que ya no existia.
            import json as _js

            Path(panel.cfg.almacen.estado).write_text(_js.dumps({
                "inicio": "2026-07-27T10:00:00+00:00",
                "fin": "2026-07-27T10:00:18+00:00", "terminada": True,
                "total": 1, "ok": 1, "cambios": 0, "fallidos": 0,
                "equipos": [{"nombre": "Fantasma", "ip": "10.20.0.77",
                             "grupo": "core", "empresa": "Empresa Uno",
                             "estado": "sin_cambios", "detalle": "",
                             "intento": 1, "fin": ""}],
                "historial": [],
            }), encoding="utf-8")

            codigo, cuerpo = panel.pedir("/api/estado")
            datos = _js.loads(cuerpo)
            nombres = [e["nombre"] for e in datos.get("equipos", [])]
            comprobar(f"un equipo que ya no esta en el inventario desaparece "
                      f"({nombres})", "Fantasma" not in nombres)
            comprobar("y deja de contar en el total",
                      datos.get("total") == len(nombres))
            comprobar("los que si estan salen, aunque el ciclo no los tocara",
                      "Equipo-Uno" in nombres)
            uno = next((e for e in datos["equipos"] if e["nombre"] == "Equipo-Uno"),
                       {})
            comprobar("y salen como pendientes, que es lo que son",
                      uno.get("estado") == "pendiente")
            comprobar("sin contarse como respaldados", datos.get("ok") == 0)
            Path(panel.cfg.almacen.estado).unlink(missing_ok=True)

            print("\n--- El historial de un equipo dado de baja ---")
            # Dar de baja NO borra los respaldos: siguen saliendo en Cambios, y
            # desde ahi se llega a "Todas las versiones". Ese boton buscaba la
            # ficha en el inventario y contestaba 404 sobre un equipo cuyos
            # datos estan delante: un error donde no hay ningun error.
            panel.pedir("/equipos/nuevo", {**EQUIPO_MUDO, "nombre": "Equipo-Baja",
                                           "ip": "10.20.0.9", "puerto": "22"})
            panel.pedir("/equipos/baja", {"nombre": "Equipo-Baja"})
            codigo, cuerpo = panel.pedir("/historial?equipo=Equipo-Baja")
            comprobar(f"no contesta 404 (dio {codigo})", codigo != 404)
            comprobar("vuelve a Cambios, que es de donde se venia",
                      codigo == 303 or "/cambios" in cuerpo)

            codigo, html = panel.pedir(
                "/cambios?ok=" + urllib.parse.quote("Equipo-Baja ya no esta"))
            comprobar(f"y Cambios explica por que (dio {codigo})", codigo == 200)
            comprobar("con el aviso a la vista",
                      "Equipo-Baja ya no esta" in html)

            print("\n--- Borrar los datos de un equipo dado de baja ---")
            # Se fabrica lo que deja un equipo al que se dio de baja despues de
            # haberlo respaldado: un .rsc en el repositorio del que ya no
            # responde ninguna ficha del inventario. Es lo unico que esta
            # pantalla puede borrar.
            from mkbackup.device import Resultado
            from mkbackup.inventory import Equipo
            from mkbackup.store import Almacen

            almacen = Almacen(panel.cfg)
            almacen.inicializar()
            ido = Equipo(nombre="Equipo-Ido", ip="10.20.0.44",
                         empresa="Empresa Uno")
            almacen.guardar(Resultado(equipo=ido,
                                      export="/ip dns\nset servers=8.8.8.8\n"))
            archivo = Path(panel.cfg.almacen.git) / ido.ruta_relativa
            comprobar("el equipo dado de baja dejo su respaldo", archivo.is_file())

            codigo, html = panel.pedir("/ajustes")
            comprobar("Ajustes ofrece borrarlo",
                      'action="/ajustes/datos/borrar"' in html
                      and "Equipo-Ido" in html)
            comprobar("y no ofrece borrar al que sigue de alta",
                      'value="empresa-uno/Equipo-Uno.rsc"' not in html)

            # Escribir mal el nombre es el unico freno que hay entre este boton
            # y perder los respaldos de otro. Tiene que parar de verdad.
            codigo, html = panel.pedir(
                "/ajustes/datos/borrar",
                {"ruta": ido.ruta_relativa, "modo": "purgar",
                 "confirmacion": "equipo-ido"},
            )
            comprobar(f"con la confirmacion mal escrita se niega (dio {codigo})",
                      codigo == 400)
            comprobar("y dice que hay que escribir el nombre exacto",
                      "nombre exacto" in html)
            comprobar("sin haber borrado nada", archivo.is_file())

            # Una ruta que no esta en la lista tampoco vale, aunque la mande el
            # formulario: un formulario se edita.
            codigo, _ = panel.pedir(
                "/ajustes/datos/borrar",
                {"ruta": "empresa-uno/Equipo-Uno.rsc", "modo": "retirar",
                 "confirmacion": "Equipo-Uno"},
            )
            comprobar(f"una ruta que no esta dada de baja se rechaza (dio {codigo})",
                      codigo == 404)

            codigo, html = panel.pedir(
                "/ajustes/datos/borrar",
                {"ruta": ido.ruta_relativa, "modo": "retirar",
                 "confirmacion": "Equipo-Ido"},
            )
            comprobar(f"con el nombre bien escrito borra (dio {codigo})",
                      codigo == 200)
            comprobar("el respaldo ya no esta en el repositorio",
                      not archivo.exists())
            comprobar("y el panel dice que se puede recuperar",
                      "recuperar" in html)

            print("\n--- Un error no deja el cuerpo del POST en el socket ---")
            # Casi todos los caminos de error cortan ANTES de leer el cuerpo:
            # "no tienes permiso", "solo lectura", "esa pagina no existe". Con
            # keep-alive, esos bytes sin leer los interpreta la peticion
            # SIGUIENTE como su primera linea, y la conexion se cae a la mitad
            # mientras el registro dice que se contesto bien. Ya paso una vez.
            import socket

            cuerpo = urllib.parse.urlencode({"relleno": "x" * 500}).encode()
            crudo = (
                b"POST /no-existe HTTP/1.1\r\nHost: x\r\n"
                + f"Cookie: {panel.cookie}\r\n".encode()
                + b"Content-Type: application/x-www-form-urlencoded\r\n"
                + f"Content-Length: {len(cuerpo)}\r\n\r\n".encode()
                + cuerpo
            )
            s = socket.create_connection(
                ("127.0.0.1", panel.srv.server_address[1]), timeout=10)
            try:
                s.sendall(crudo)
                respuesta = b""
                while b"\r\n\r\n" not in respuesta and len(respuesta) < 8192:
                    trozo = s.recv(4096)
                    if not trozo:
                        break
                    respuesta += trozo
            finally:
                s.close()
            cabeceras = respuesta.split(b"\r\n\r\n", 1)[0].lower()
            comprobar("el error contesta 404", b" 404 " in respuesta[:32])
            comprobar("y cierra la conexion, para que esos bytes no los lea "
                      "la peticion siguiente",
                      b"connection: close" in cabeceras)

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
