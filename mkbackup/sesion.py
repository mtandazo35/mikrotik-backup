"""Autenticacion del panel: hash de la clave y sesiones en memoria.

Dos piezas, separadas a proposito:

  - `hashear` / `verificar`: la clave del panel NO se guarda en claro. Lo que
    se guarda (en el archivo de usuarios, o en el YAML de las versiones
    antiguas) es un pbkdf2-sha256 con sal. Quien lea esos archivos (un backup
    del /root, un vistazo por encima del hombro) no se lleva la clave.

  - `Sesiones`: tokens opacos en memoria, cada uno con el nombre de la cuenta
    que lo abrio. No son cookies firmadas y es deliberado: una cookie firmada
    no se puede revocar sin llevar una lista igualmente, y aqui "Salir" -y
    sobre todo echar a alguien- tiene que cerrar la sesion de verdad. El precio
    es que reiniciar el panel cierra las sesiones abiertas, que en un servicio
    de consulta es un inconveniente de un segundo.

Todo con la libreria estandar (hashlib, hmac, secrets), igual que el resto del
proyecto: ni una dependencia mas en un servicio desatendido.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

ALGORITMO = "pbkdf2_sha256"
# Coste del hash. A mas iteraciones, mas caro probar claves por fuerza bruta;
# el precio es una espera en cada login, que aqui es una vez cada varias horas.
ITERACIONES = 240_000
SAL_BYTES = 16
# Longitud del token de sesion. 32 bytes al azar no se adivinan.
TOKEN_BYTES = 32

COOKIE = "mkbackup_sesion"


# --- Clave ------------------------------------------------------------------


def _b64(datos: bytes) -> str:
    return base64.b64encode(datos).decode("ascii")


def hashear(clave: str, iteraciones: int = ITERACIONES, sal: bytes | None = None) -> str:
    """Devuelve 'pbkdf2_sha256$iteraciones$sal$hash', listo para el YAML."""
    if sal is None:
        sal = secrets.token_bytes(SAL_BYTES)
    bruto = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, iteraciones)
    return f"{ALGORITMO}${iteraciones}${_b64(sal)}${_b64(bruto)}"


def formato_valido(guardado: str) -> bool:
    """Si el valor del YAML tiene pinta de hash de los nuestros.

    Se comprueba al cargar la configuracion y no al primer login: enterarse de
    que web.clave_hash esta mal escrito cuando alguien necesita mirar el panel
    es enterarse tarde.
    """
    partes = guardado.split("$")
    if len(partes) != 4 or partes[0] != ALGORITMO:
        return False
    if not partes[1].isdigit() or int(partes[1]) < 1:
        return False
    try:
        # binascii.Error hereda de ValueError, con esto basta.
        base64.b64decode(partes[2], validate=True)
        base64.b64decode(partes[3], validate=True)
    except ValueError:
        return False
    return True


def verificar(clave: str, guardado: str) -> bool:
    """Comprueba la clave contra el hash del YAML. Nunca lanza."""
    if not formato_valido(guardado):
        return False
    _, iteraciones, sal_b64, hash_b64 = guardado.split("$")
    try:
        sal = base64.b64decode(sal_b64, validate=True)
        esperado = base64.b64decode(hash_b64, validate=True)
    except ValueError:
        return False
    calculado = hashlib.pbkdf2_hmac(
        "sha256", clave.encode("utf-8"), sal, int(iteraciones)
    )
    # compare_digest y no ==: comparar byte a byte tarda distinto segun cuanto
    # coincide, y ese tiempo se puede medir para ir adivinando el hash.
    return hmac.compare_digest(calculado, esperado)


# --- Sesiones ---------------------------------------------------------------


class Sesiones:
    """Tokens abiertos y intentos fallidos. Seguro para usar desde varios hilos.

    El panel corre sobre ThreadingHTTPServer: cada pestana abierta pide el
    estado cada pocos segundos desde su propio hilo, asi que todo pasa por el
    mismo lock.

    Cada token lleva pegado el NOMBRE de la cuenta que lo abrio. Con un unico
    usuario bastaba con saber si el token valia; con cuentas y roles hay que
    saber QUIEN es en cada peticion, para decidir que puede hacer y para que el
    log diga quien toco cada cosa.
    """

    def __init__(
        self,
        duracion_horas: int = 8,
        inactividad_minutos: int = 30,
        intentos_max: int = 5,
        bloqueo_segundos: int = 300,
    ):
        # Dos relojes, y hacen falta los dos.
        #
        # `duracion` es el tope absoluto: pase lo que pase, a las N horas hay
        # que volver a escribir la clave. `inactividad` es el que se reinicia
        # con cada uso: media hora sin tocar el panel y la sesion se cierra
        # sola. Solo el primero deja una sesion abierta toda la jornada en un
        # ordenador que alguien dejo desbloqueado; solo el segundo deja una
        # sesion viva indefinidamente mientras se use, que es justo lo que no
        # se quiere de una credencial robada.
        self.duracion = duracion_horas * 3600
        self.inactividad = inactividad_minutos * 60
        self.intentos_max = intentos_max
        self.bloqueo = bloqueo_segundos
        self._lock = threading.Lock()
        # token -> (usuario, tope absoluto, tope por quietud)
        self._abiertas: dict[str, tuple[str, float, float]] = {}
        self._fallos: dict[str, tuple[int, float]] = {}  # ip -> (cuantos, ultimo)

    # monotonic y no time(): un cambio de hora del sistema (NTP, horario de
    # verano) no debe alargar ni cortar sesiones.
    @staticmethod
    def _ahora() -> float:
        return time.monotonic()

    def abrir(self, usuario: str) -> str:
        """Abre sesion para esa cuenta y devuelve su token."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        ahora = self._ahora()
        with self._lock:
            self._purgar()
            self._abiertas[token] = (
                usuario, ahora + self.duracion, ahora + self.inactividad
            )
        return token

    def valida(self, token: str | None, refrescar: bool = True) -> str | None:
        """Nombre del usuario dueno del token, o None si no vale.

        Devuelve el nombre y no un bool porque quien pregunta (cada peticion
        del panel) necesita justo eso a continuacion: mirar el rol de esa
        cuenta. Si devolviera bool habria que llevar el nombre por otro lado.

        `refrescar` es lo que distingue "usar el panel" de "tener el panel
        abierto", y de eso depende que la caducidad por inactividad sirva de
        algo. La pantalla de Estado le pregunta a /api/estado cada pocos
        segundos por su cuenta, sin que nadie toque nada: si esas peticiones
        reiniciaran el reloj, una pestana olvidada en un ordenador encendido
        mantendria la sesion viva para siempre, que es exactamente la situacion
        contra la que existe el tope. Quien llama pasa refrescar=False para
        esas rutas (ver web._usuario).
        """
        if not token:
            return None
        with self._lock:
            entrada = self._abiertas.get(token)
            if entrada is None:
                return None
            usuario, tope, quietud = entrada
            ahora = self._ahora()
            if tope <= ahora or quietud <= ahora:
                del self._abiertas[token]
                return None
            if refrescar:
                self._abiertas[token] = (usuario, tope, ahora + self.inactividad)
            return usuario

    def cerrar(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._abiertas.pop(token, None)

    def cerrar_usuario(self, nombre: str) -> int:
        """Cierra TODAS las sesiones abiertas de esa cuenta. Devuelve cuantas.

        Es la otra mitad de administrar cuentas, y no es un extra: cuando a
        alguien se le cambia la clave, se le baja el rol o se le borra la
        cuenta, lo que hay guardado en el archivo de usuarios cambia, pero su
        token sigue en memoria y sigue valiendo. Sin esto, al que acabas de
        echar (o de degradar) le quedan hasta 8 horas dentro con los permisos
        de antes, que es justo el rato en el que hace falta que no los tenga.
        """
        if not nombre:
            return 0
        with self._lock:
            tokens = [t for t, (u, *_) in self._abiertas.items() if u == nombre]
            for token in tokens:
                del self._abiertas[token]
            return len(tokens)

    def cerrar_todas(self) -> int:
        """Cierra TODAS las sesiones abiertas. Devuelve cuantas eran.

        Existe por la restauracion de una copia: al volcar el paquete se
        reemplaza el archivo de cuentas entero, asi que los tokens que hay en
        memoria apuntan a usuarios que quiza ya no existen, o que existen con
        otro rol y otro alcance. Dejarlos vivos seria mantener dentro del panel
        -con los permisos de antes- a gente cuyo archivo de cuentas acaba de
        desaparecer.
        """
        with self._lock:
            cuantas = len(self._abiertas)
            self._abiertas.clear()
            return cuantas

    def _purgar(self) -> None:
        """Tira lo caducado. Debe llamarse con el lock tomado."""
        ahora = self._ahora()
        for token in [
            t for t, (_, tope, quietud) in self._abiertas.items()
            if tope <= ahora or quietud <= ahora
        ]:
            del self._abiertas[token]
        for ip in [
            i for i, (_, ultimo) in self._fallos.items()
            if ultimo + self.bloqueo <= ahora
        ]:
            del self._fallos[ip]

    # --- Freno a la fuerza bruta -------------------------------------------

    def bloqueado(self, ip: str) -> int:
        """Segundos que le quedan a esta IP de castigo (0 si puede intentar)."""
        with self._lock:
            cuantos, ultimo = self._fallos.get(ip, (0, 0.0))
            if cuantos < self.intentos_max:
                return 0
            restante = ultimo + self.bloqueo - self._ahora()
            if restante <= 0:
                del self._fallos[ip]
                return 0
            return int(restante) + 1

    def vigiladas(self) -> list[dict]:
        """Las IPs con intentos fallidos vivos, la mas castigada primero.

        Se devuelven TAMBIEN las que aun no llegan al limite. Ver "3 de 5
        intentos" en una direccion es lo que permite darse cuenta de que algo
        esta pasando antes de que el bloqueo salte, y sirve para distinguir a
        quien se equivoco de tecla de quien esta probando claves.
        """
        with self._lock:
            self._purgar()
            ahora = self._ahora()
            filas = []
            for ip, (cuantos, ultimo) in self._fallos.items():
                bloqueada = cuantos >= self.intentos_max
                restan = int(ultimo + self.bloqueo - ahora) + 1 if bloqueada else 0
                filas.append({
                    "ip": ip,
                    "intentos": cuantos,
                    "bloqueada": bloqueada,
                    "restantes": max(0, restan),
                })
            return sorted(filas, key=lambda f: (-f["intentos"], f["ip"]))

    def desbloquear(self, ip: str) -> bool:
        """Borra los intentos de esa IP. True si habia algo que borrar.

        Es la salida para un bloqueo injusto: alguien que se equivoco cinco
        veces de teclado no tiene por que esperar cinco minutos, y sin esto la
        unica forma de levantarlo seria reiniciar el panel, que echa de paso a
        todos los que estuvieran dentro.
        """
        with self._lock:
            return self._fallos.pop(ip, None) is not None

    def intentos(self, ip: str) -> int:
        """Cuantos fallos lleva esa IP en la ventana actual.

        Lo usa la auditoria para poder escribir "intento 3 de 5": un intento
        suelto no dice nada, pero la cuenta atras hacia el bloqueo es lo que
        distingue un dedo torpe de alguien probando claves.
        """
        with self._lock:
            return self._fallos.get(ip, (0, 0.0))[0]

    def fallo(self, ip: str) -> None:
        with self._lock:
            # Se purga tambien aqui, y no solo en abrir(): _fallos SOLO crece en
            # este metodo, asi que si nadie llega a entrar nunca (que es
            # exactamente lo que pasa mientras alguien prueba claves desde una
            # IP distinta cada vez) el diccionario no se limpiaba jamas. Purgar
            # en el mismo sitio donde crece lo deja acotado a las IPs que han
            # fallado dentro de la ventana de bloqueo. Sale gratis: un fallo de
            # login ya cuesta un pbkdf2 de medio segundo, y ocurre de tarde en
            # tarde, asi que recorrer un diccionario pequeno no se nota.
            self._purgar()
            cuantos, _ = self._fallos.get(ip, (0, 0.0))
            self._fallos[ip] = (cuantos + 1, self._ahora())

    def acierto(self, ip: str) -> None:
        with self._lock:
            self._fallos.pop(ip, None)
