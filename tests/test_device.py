"""Prueba de la conexion a un MikroTik: clasificacion de fallos y lectura del export.

Lo que se comprueba aqui sale de haber enchufado esto a un equipo de verdad:

  - un router con lista de direcciones ('/ip service set ssh address=...') NO
    rechaza la conexion: acepta el TCP y lo cierra sin mandar su banner. paramiko
    lo envuelve en un SSHException generico y sin ayuda se queda en "error SSH",
    que manda a revisar la configuracion de SSH cuando lo que hay que tocar es
    una ACL. Es EL fallo del dia uno al dar de alta un servidor de respaldos.
  - el modelo se lee de dos sitios que dan respuestas DISTINTAS para el mismo
    aparato ('hEX' en board-name, 'RB750Gr3' en la cabecera del export), asi que
    cual manda no puede quedar al azar.
  - el ruido del export (fechas, avisos intermitentes) no puede colarse como un
    cambio de configuracion, o cada ciclo genera un commit vacio.
  - nada de lo que se guarda como nombre de archivo puede salirse del repo.

El caso del saludo cortado se levanta aqui mismo con un socket que acepta y
cierra: es exactamente lo que hace el router, y no hace falta hardware.

Ejecutar:  python -m tests.test_device
"""

import logging
import socket
import threading

import paramiko

from mkbackup import device as dv
from mkbackup.config import Config
from mkbackup.inventory import Equipo

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


class PuertoQueCuelga:
    """Acepta la conexion y la cierra sin hablar: un MikroTik con ACL puesta.

    Se cierra el socket a pelo, sin mandar nada. Segun lo rapido que vaya la
    maquina, el otro extremo vera un reset o un cierre limpio; los dos casos
    acaban en el mismo SSHException de paramiko, que es justo lo que se quiere
    comprobar que sabemos clasificar.
    """

    def __init__(self):
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.puerto = self._srv.getsockname()[1]
        self._vivo = True
        self._hilo = threading.Thread(target=self._servir, daemon=True)
        self._hilo.start()

    def _servir(self):
        while self._vivo:
            try:
                cliente, _ = self._srv.accept()
            except OSError:
                return
            try:
                cliente.close()
            except OSError:
                pass

    def cerrar(self):
        self._vivo = False
        try:
            self._srv.close()
        except OSError:
            pass


def _config() -> Config:
    cfg = Config()
    cfg.ssh.usuario = "prueba"
    cfg.ssh.password = "prueba"
    cfg.ssh.timeout = 5
    cfg.ssh.host_key_desconocida = "aceptar"
    return cfg


def main() -> None:
    # Lo primero, y no por orden: sin esto el hilo de fondo de paramiko vuelca
    # el traceback del apartado 1 en medio de la salida de la prueba. Que
    # llamarlo lo calle ya es media comprobacion; la otra media va en el 2.
    from mkbackup.cli import _configurar_log

    _configurar_log(verboso=False)

    # --- 1. El saludo que nunca llega --------------------------------------
    puerto_mudo = PuertoQueCuelga()
    try:
        eq = Equipo(
            nombre="Con-ACL", empresa="Pruebas", ip="127.0.0.1",
            puerto=puerto_mudo.puerto, grupo="p",
        )
        try:
            with dv.Conexion(eq, _config()):
                pass
            comprobar("un puerto que acepta y cierra tiene que fallar", False)
        except dv.ErrorEquipo as exc:
            comprobar(
                "acepta el TCP y cierra sin banner -> SIN_CONEXION, no DESCONOCIDO",
                exc.tipo is dv.TipoError.SIN_CONEXION,
            )
            comprobar(
                "el mensaje dice que lo cerro sin identificarse",
                "sin identificarse" in exc.mensaje,
            )
            comprobar(
                "el mensaje manda a mirar la lista de direcciones del router",
                "/ip service print" in exc.mensaje,
            )
            comprobar(
                "el mensaje trae el puerto que se intento",
                str(puerto_mudo.puerto) in exc.mensaje,
            )
            comprobar(
                "no se cuela la contrasena en el mensaje",
                "prueba" not in exc.mensaje.replace("Pruebas", ""),
            )
    finally:
        puerto_mudo.cerrar()

    comprobar(
        "se reconoce el texto de paramiko para el banner",
        dv._es_saludo_cortado(
            paramiko.SSHException("Error reading SSH protocol banner")
        ),
    )
    comprobar(
        "se reconoce tambien con la causa pegada detras, como lo escribe paramiko",
        dv._es_saludo_cortado(
            paramiko.SSHException(
                "Error reading SSH protocol banner[Errno 104] Connection reset by peer"
            )
        ),
    )
    comprobar(
        "un fallo SSH cualquiera NO se confunde con el del banner",
        not dv._es_saludo_cortado(
            paramiko.SSHException("Incompatible ssh peer (no acceptable kex)")
        ),
    )

    # --- 2. Nadie escribe en el log por detras del censor -------------------
    # El hilo de fondo de paramiko volcaba el traceback entero sin pasar por
    # _censurar, que es la premisa sobre la que esta escrito todo device.py.
    comprobar(
        "sin -v, paramiko.transport no puede volcar sus tracebacks",
        not logging.getLogger("paramiko.transport").isEnabledFor(logging.ERROR),
    )
    _configurar_log(verboso=True)
    comprobar(
        "con -v si habla: ahi hay alguien mirando a proposito",
        logging.getLogger("paramiko.transport").isEnabledFor(logging.ERROR),
    )
    comprobar(
        "pero nunca a DEBUG, que vuelca los paquetes en crudo",
        not logging.getLogger("paramiko.transport").isEnabledFor(logging.DEBUG),
    )

    # --- 3. Version y modelo, con la salida REAL de un hEX ------------------
    # Copiada tal cual de un RB750Gr3 con RouterOS 7.22.1. El detalle que
    # importa: board-name dice 'hEX' y la cabecera del export dice 'RB750Gr3'.
    # Son el mismo aparato con dos nombres, y manda board-name (existe en v6 y
    # en v7; la cabecera se anadio en las v7 y ya ha cambiado de forma).
    recursos_reales = """                   uptime: 2w4d14h35m20s
                  version: 7.22.1 (stable)
               build-time: 2026-03-23 14:35:15
         factory-software: 6.46.3
              free-memory: 162.3MiB
              total-memory: 256.0MiB
                      cpu: MIPS 1004Kc V2.15
        architecture-name: mmips
               board-name: hEX
                 platform: MikroTik
"""
    comprobar(
        "la version sale del resource print aunque venga con '(stable)' detras",
        dv._version_desde(recursos_reales) == "7.22.1",
    )
    comprobar(
        "el modelo sale de board-name",
        dv._modelo_desde(recursos_reales) == "hEX",
    )
    comprobar(
        "la cabecera del export da el otro nombre del mismo aparato",
        dv.modelo_desde_export("# model = RB750Gr3\n") == "RB750Gr3",
    )
    comprobar(
        "sin board-name no se inventa nada",
        dv._modelo_desde("version: 7.1\n") == "",
    )
    comprobar(
        "una queja del router no se guarda como modelo",
        dv.sanear_modelo("no such item (4)") == "",
    )
    comprobar(
        "un equipo que contesta una parrafada no ensancha la tabla del panel",
        len(dv.sanear_modelo("X" * 500)) == dv.LARGO_MAX_MODELO,
    )

    # --- 4. El ruido del export no puede parecer un cambio ------------------
    ruidoso = (
        "# 2026-07-27 15:13:08 by RouterOS 7.22.1\n"
        "# model = RB750Gr3\n"
        "/interface list add name=WAN\n"
        "# inactive time out\n"
        "# poe-out status: short_circuit\n"
        "/ip pool add name=dhcp_pool0 ranges=172.16.100.2\n"
        "\n\n\n"
    )
    limpio_1 = dv.limpiar_export(ruidoso)
    limpio_2 = dv.limpiar_export(ruidoso.replace("15:13:08", "19:41:02"))
    comprobar(
        "dos exports que solo cambian en la fecha salen identicos",
        limpio_1 == limpio_2,
    )
    comprobar(
        "el ruido intermitente se va",
        "poe-out" not in limpio_1 and "inactive time" not in limpio_1,
    )
    comprobar(
        "la configuracion de verdad se queda",
        "/interface list add name=WAN" in limpio_1
        and "dhcp_pool0" in limpio_1,
    )
    comprobar(
        "termina en un unico salto de linea",
        limpio_1.endswith("\n") and not limpio_1.endswith("\n\n"),
    )
    comprobar(
        "las lineas partidas con '\\' se reunen para que el diff se lea",
        dv.limpiar_export("/ip firewall add chain=x \\\n    action=drop\n")
        == "/ip firewall add chain=x action=drop\n",
    )

    # --- 5. La identidad acaba siendo una ruta del repo ---------------------
    comprobar(
        "la identidad sale del export en una linea (terse)",
        dv.identidad_desde_export("/system identity set name=Mikrotik-Pruebas-Juan\n")
        == "Mikrotik-Pruebas-Juan",
    )
    comprobar(
        "y tambien en dos lineas, como la escribe el export sin terse",
        dv.identidad_desde_export('/system identity\nset name="Router Quito"\n')
        == "Router Quito",
    )
    comprobar(
        "los espacios pasan a guion y no se pierde la frontera entre palabras",
        dv.sanear_nombre("Router Principal Quito") == "Router-Principal-Quito",
    )
    comprobar(
        "las tildes se aplanan sin perder la letra",
        dv.sanear_nombre("Router-Península-Ñ") == "Router-Peninsula-N",
    )
    for veneno, motivo in (
        ("../../etc/passwd", "no se sale del repo"),
        ("..", "no es el directorio padre"),
        (".", "no es el directorio actual"),
        ("/etc/shadow", "no es una ruta absoluta"),
        ("nombre.", "Windows se come el punto final"),
        ("", "vacio se queda vacio"),
        ("Ж", "un nombre que no deja nada usable no inventa un nombre"),
    ):
        salido = dv.sanear_nombre(veneno)
        # ascii() y no !r: la consola de Windows va en cp1252 y un repr con
        # cirilico dentro revienta el print, o sea que la prueba se caeria por
        # como se cuenta lo que hace y no por lo que hace.
        comprobar(
            f"{motivo}: {ascii(veneno)} -> {ascii(salido)}",
            salido in ("", "etc-passwd", "etc-shadow", "nombre"),
        )
    comprobar(
        "una identidad kilometrica no decide el ancho de las rutas del repo",
        len(dv.sanear_nombre("R" * 500)) == dv.LARGO_MAX_NOMBRE,
    )

    # --- 6. La respuesta que es una queja, no un valor ----------------------
    comprobar(
        "'syntax error' no se guarda como nombre del equipo",
        dv._valor_unico("expected end of command") == "",
    )
    comprobar(
        "varias lineas no son una identidad",
        dv._valor_unico("uno\ndos") == "",
    )
    comprobar(
        "una linea limpia si lo es",
        dv._valor_unico("  Mikrotik-Pruebas-Juan  ") == "Mikrotik-Pruebas-Juan",
    )

    # --- 7. Credenciales: un campo vacio hereda, uno lleno manda ------------
    cfg = _config()
    cfg.ssh.usuario, cfg.ssh.password = "general", "clave-general"
    cfg.ssh.clave_privada = "/root/.ssh/id_ed25519"

    base = dict(nombre="X", empresa="P", ip="10.0.0.1", puerto=22, grupo="g")
    usuario, clave, privada = dv._credenciales(Equipo(**base), cfg)
    comprobar(
        "sin nada propio se heredan las generales, llave incluida",
        (usuario, clave, privada) == ("general", "clave-general",
                                      "/root/.ssh/id_ed25519"),
    )
    usuario, clave, privada = dv._credenciales(
        Equipo(**base, usuario="cliente"), cfg
    )
    comprobar(
        "solo el usuario propio: la clave general se sigue usando",
        (usuario, clave) == ("cliente", "clave-general"),
    )
    usuario, clave, privada = dv._credenciales(
        Equipo(**base, usuario="cliente", clave="suya"), cfg
    )
    comprobar(
        "con clave propia se entra SOLO por contrasena: la llave global se descarta",
        (usuario, clave, privada) == ("cliente", "suya", ""),
    )

    # --- 8. Los secretos no salen en los mensajes ---------------------------
    cfg.binario.password = "clave-del-backup"
    con = dv.Conexion(Equipo(**base, usuario="c", clave="s3cr3t0"), cfg)
    comprobar(
        "la contrasena del equipo se tapa",
        con._limpio("fallo con s3cr3t0 dentro") == "fallo con *** dentro",
    )
    comprobar(
        "y la del backup binario, que viaja dentro del comando",
        "clave-del-backup" not in con._limpio(
            "/system backup save password=clave-del-backup"
        ),
    )
    comprobar(
        "la clave general NO se tapa si este equipo no la usa (no hay que tapar de mas)",
        con._limpio("clave-general") == "clave-general",
    )

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
