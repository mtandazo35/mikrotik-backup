"""Registro de auditoria: quien entro, quien lo intento y quien toco que.

Es distinto del log del proceso, y por eso es un archivo aparte:

  - El log del proceso (journald) esta pensado para diagnosticar: se rota, se
    mezcla con el ruido de los respaldos y nadie lo lee salvo cuando algo va
    mal. Este archivo esta pensado para responder "quien" y "cuando", que es
    la pregunta que se hace despues de un incidente.
  - Una linea por evento, en JSON, para poder filtrarla sin analizar texto
    libre. Anadir campos manana no rompe a quien lea las lineas de hoy.
  - Nunca contiene claves, ni hashes, ni tokens de sesion. Si algun dia hace
    falta anotar algo de una credencial, se anota que cambio, no su valor.

Lo escribe el panel; el respaldo no lo toca. Se guarda junto al resto de los
datos, con permisos 0600, porque la lista de quien administra la red y desde
que direcciones tambien es informacion util para quien quiera atacarla.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("mkbackup.auditoria")

# Etiquetas legibles. La clave es lo que se guarda (estable, para poder
# filtrar); el texto es lo que se ensena y puede cambiar sin romper nada.
EVENTOS = {
    "login_ok": "Entro al panel",
    "login_fallido": "Intento fallido",
    "login_bloqueado": "Bloqueado por intentos",
    "desbloqueo": "Desbloqueo una direccion",
    "salir": "Cerro sesion",
    "sin_permiso": "Intento algo sin permiso",
    "fuera_de_alcance": "Intento ver algo fuera de su alcance",
    "clave_propia": "Cambio su propia clave",
    "usuario_alta": "Creo una cuenta",
    "usuario_cambio": "Modifico una cuenta",
    "usuario_baja": "Borro una cuenta",
    "rol_cambio": "Modifico un rol",
    "rol_baja": "Borro un rol",
    "equipo_alta": "Dio de alta un equipo",
    "equipo_cambio": "Edito un equipo",
    "equipo_baja": "Dio de baja un equipo",
    "importacion": "Importo equipos",
    "ajustes": "Cambio los ajustes",
    "acceso_ssh": "Cambio el acceso a los routers",
}

# Eventos que conviene mirar aunque no haya pasado nada mas: son los que
# dibujan un intento de entrar sin permiso.
SOSPECHOSOS = ("login_fallido", "login_bloqueado", "sin_permiso", "fuera_de_alcance")

# A partir de este tamano se rota. Con una linea de ~150 bytes son del orden de
# 30.000 eventos, meses de uso normal en un panel de esto.
MAXIMO_BYTES = 5 * 1024 * 1024


def _limpio(texto) -> str:
    """Deja el valor en una sola linea y sin caracteres de control.

    Un nombre de usuario llega de un formulario, asi que puede traer saltos de
    linea: sin esto, quien lo teclee puede inventarse lineas enteras del
    registro y hacer que parezca que entro otra persona. Tambien se cortan los
    escapes de terminal, porque este archivo se lee con `tail` o con `cat` y
    ahi una secuencia de escape puede borrar de la pantalla lo que acaba de
    pasar.
    """
    limpio = "".join(
        c for c in str(texto or "") if c.isprintable() and c not in "\r\n"
    )
    return limpio[:200]


class Auditoria:
    """Escribe y lee el registro. Seguro para usar desde varios hilos."""

    def __init__(self, ruta, maximo_bytes: int = MAXIMO_BYTES):
        self.ruta = Path(ruta)
        self.maximo = maximo_bytes
        self._lock = threading.Lock()

    # --- Escritura ----------------------------------------------------------

    def anotar(self, evento: str, usuario: str = "", ip: str = "",
               detalle: str = "") -> None:
        """Anade un evento. NUNCA lanza: auditar no puede tumbar el panel.

        Que un fallo al escribir aqui impidiera dar de alta un equipo seria
        cambiar un problema de registro por uno de servicio. Se avisa por el
        log del proceso y se sigue.
        """
        linea = json.dumps(
            {
                "cuando": datetime.now(timezone.utc).isoformat(),
                "evento": _limpio(evento),
                "usuario": _limpio(usuario),
                "ip": _limpio(ip),
                "detalle": _limpio(detalle),
            },
            ensure_ascii=False,
        )
        try:
            with self._lock:
                self._rotar_si_toca()
                self.ruta.parent.mkdir(parents=True, exist_ok=True)
                nuevo = not self.ruta.exists()
                with self.ruta.open("a", encoding="utf-8") as fh:
                    fh.write(linea + "\n")
                if nuevo:
                    # Solo al crearlo: hacerlo en cada escritura seria una
                    # llamada al sistema de mas por evento, para nada.
                    try:
                        os.chmod(self.ruta, 0o600)
                    except OSError:
                        pass
        except OSError as exc:
            log.warning("No se pudo escribir la auditoria: %s", exc)

    def _rotar_si_toca(self) -> None:
        """Deja el archivo en un tamano razonable. Con el lock tomado.

        Se conserva UNA generacion anterior. Guardar mas seria inventar una
        politica de retencion que el sistema no tiene; quien necesite historia
        larga que se lleve el archivo a donde guarde los logs de verdad.
        """
        try:
            if self.ruta.exists() and self.ruta.stat().st_size >= self.maximo:
                anterior = self.ruta.with_suffix(self.ruta.suffix + ".1")
                os.replace(self.ruta, anterior)
        except OSError as exc:
            log.warning("No se pudo rotar la auditoria: %s", exc)

    # --- Lectura ------------------------------------------------------------

    def _coinciden(self, evento: str = "", usuario: str = "",
                   solo_sospechosos: bool = False) -> list[dict]:
        """Todos los eventos que casan con el filtro, recientes primero.

        Lee el archivo entero y lo recorre al reves. Con el tope de tamano de
        arriba son unos pocos megabytes y del orden de 30.000 lineas: es mas
        simple que leer hacia atras por bloques y, a esta escala, igual de
        rapido. Si algun dia el registro creciera mucho mas, este es el sitio
        que habria que cambiar, y solo este.
        """
        try:
            crudo = self.ruta.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return []

        eventos = []
        for linea in reversed(crudo.splitlines()):
            if not linea.strip():
                continue
            try:
                dato = json.loads(linea)
            except ValueError:
                # Una linea a medias (por ejemplo si se corto la luz durante
                # una escritura) no puede impedir leer el resto.
                continue
            if not isinstance(dato, dict):
                continue
            if evento and dato.get("evento") != evento:
                continue
            if usuario and dato.get("usuario") != usuario:
                continue
            if solo_sospechosos and dato.get("evento") not in SOSPECHOSOS:
                continue
            eventos.append(dato)
        return eventos

    def leer(self, maximo: int = 200, evento: str = "", usuario: str = "",
             solo_sospechosos: bool = False) -> list[dict]:
        """Los N eventos mas recientes que casan. Nunca lanza."""
        return self._coinciden(evento, usuario, solo_sospechosos)[:maximo]

    def buscar(self, desde: int = 0, cuantos: int = 50, evento: str = "",
               usuario: str = "", solo_sospechosos: bool = False):
        """Devuelve (pagina, total, desde_real).

        Las tres cosas juntas porque quien pinta la tabla necesita las tres:
        la pagina para las filas, el total para saber cuantas paginas hay, y
        el `desde` REAL para escribir "eventos 51 a 79". Contar por separado
        obligaria a leer el archivo dos veces.

        Se devuelve el `desde` que se uso de verdad y no el que llego, porque
        se acota: la pagina viene de la URL y puede pedir la 20.000. En ese
        caso se ensena la ultima pagina completa, no una tabla vacia que
        parece decir que no hay datos. Si quien pinta usara el numero que
        pidio en vez de este, el titulo mentiria.
        """
        todos = self._coinciden(evento, usuario, solo_sospechosos)
        cuantos = max(1, cuantos)
        # Al principio de una pagina, no a un evento suelto: si no, "anteriores"
        # avanzaria de 50 en 50 desde un punto descuadrado.
        ultima = max(0, (max(0, len(todos) - 1) // cuantos) * cuantos)
        desde = min(max(0, desde), ultima)
        return todos[desde:desde + cuantos], len(todos), desde

    def usuarios_vistos(self) -> list[str]:
        """Los usuarios que aparecen en el registro, para el desplegable."""
        return sorted({
            e.get("usuario", "") for e in self.leer(maximo=5000) if e.get("usuario")
        })
