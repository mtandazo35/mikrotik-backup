"""Lo ultimo que se supo de cada equipo: modelo, version y ultimo respaldo bueno.

Por que existe aparte de estado.json y aparte del inventario, que es la pregunta
que se hace cualquiera que vea un tercer archivo JSON:

  - estado.json habla del CICLO EN CURSO (o del ultimo que corrio). Con el
    planificador un ciclo ya no respalda la flota entera, sino solo a los
    equipos a los que les toca segun su intervalo: un router de sucursal con
    intervalo de 24h no aparece en la mayoria de los estados, y ahi no hay forma
    de saber que es ni cuando se respaldo bien. Este archivo, en cambio, RECUERDA
    lo de cada equipo aunque hoy no le tocara.
  - el inventario es CONFIGURACION: lo escribe una persona (o el panel) y dice
    como queremos tratar al equipo. Esto es lo OBSERVADO: lo que el propio
    RouterOS conto de si mismo. Mezclarlos haria que un dato inventado a mano
    (un modelo escrito de memoria) pese lo mismo que uno leido del equipo, y que
    cada respaldo tuviera que reescribir el CSV entero de la flota.

La fecha del ultimo respaldo bueno tampoco se puede deducir de git: un equipo
sin cambios no deja commit, asi que el ultimo commit de su archivo puede ser de
hace meses aunque se este respaldando cada cuatro horas sin fallar una vez.

Lo escribe el respaldo y lo LEE el panel; nunca al reves, igual que estado.json.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("mkbackup.hechos")

# Version del formato, como en el resto de los archivos del proyecto: el dia que
# cambie la estructura, quien lea sabra con que se esta encontrando.
FORMATO = 1

# Los textos vienen del equipo ('board-name', la cabecera del /export), asi que
# pueden traer cualquier cosa. Se recortan antes de guardarlos porque este
# archivo lo pinta el panel en una tabla y un valor de 4 KB la destroza.
LARGO_MAXIMO = 64

# Campos que se guardan por equipo. Todos son opcionales: de un equipo que nunca
# contesto solo se sabra el ultimo_intento.
CAMPOS = (
    "modelo",
    "version",
    "ultimo_intento",
    "ultimo_ok",
    "ultimo_commit",
    "resultado",
)


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _texto(valor) -> str:
    """Deja un dato del equipo apto para guardarlo y para pintarlo.

    Se quitan los caracteres de control (incluidos los saltos de linea) por el
    mismo motivo que en device.sanear_nombre: esto no lo escribimos nosotros, y
    un router puede contestar lo que quiera. Aqui no hace falta el saneado
    completo de un nombre de archivo -este valor no viaja a ninguna ruta-, basta
    con que no pueda partir una tabla ni un log.
    """
    if not isinstance(valor, str):
        valor = "" if valor is None else str(valor)
    limpio = "".join(c for c in valor if c.isprintable()).strip()
    return limpio[:LARGO_MAXIMO]


class Hechos:
    """Lo observado de cada equipo. Seguro para usar desde varios hilos.

    Los equipos se guardan segun van llegando desde el pool de ejecutar(), asi
    que las anotaciones vienen de hilos distintos: toda escritura pasa por el
    mismo lock y reescribe el archivo completo. Con 300 equipos el JSON son unas
    pocas decenas de KB, irrelevante al lado del SSH que lo precede.

    No se guarda una copia en memoria como cache: el panel es OTRO proceso y una
    cache le daria datos viejos, y dentro del proceso de respaldo el archivo se
    reescribe entero en cada anotacion de todas formas.
    """

    def __init__(self, ruta):
        self.ruta = Path(ruta)
        self._lock = threading.Lock()
        # Ultimo contenido que se pudo leer o escribir. Solo se usa como red si
        # una lectura falla por un error del sistema de archivos: sin esto, un
        # error transitorio al leer haria que la siguiente escritura publicara
        # un archivo con un unico equipo y borrara lo que se sabia del resto.
        self._ultimo: dict[str, dict] = {}

    # --- Escritura ----------------------------------------------------------

    def anotar(
        self,
        nombre: str,
        *,
        modelo: str = "",
        version: str = "",
        resultado: str = "",
        commit: str = "",
        ok: bool = False,
    ) -> None:
        """Apunta lo que se acaba de observar de un equipo.

        LA REGLA DEL MODULO: un dato VACIO NUNCA pisa uno bueno. Si el ciclo de
        hoy fallo y no se pudo leer el modelo, se conserva el que ya se sabia. Un
        equipo apagado no puede "olvidar" que es un RB4011: justo cuando algo
        lleva fallando es cuando hace falta saber que modelo y que version tiene
        el aparato al que hay que ir. Por eso cada campo se copia solo si trae
        algo, y por eso quien llama debe pasar cadenas vacias (nunca None) para
        lo que no sabe.

        ultimo_intento se actualiza SIEMPRE y ultimo_ok solo cuando ok es True.
        Esa pareja es la que permite al panel decir "se intento hace 5 minutos
        pero el ultimo bueno fue anteayer", que es la informacion que se busca
        cuando un equipo lleva un rato sin responder. Si ultimo_ok se moviera en
        cada intento, un equipo muerto pareceria recien respaldado.
        """
        nombre = _texto(nombre)
        if not nombre:
            return

        with self._lock:
            equipos = self._cargar()
            datos = dict(equipos.get(nombre) or {})
            momento = _ahora()

            modelo = _texto(modelo)
            version = _texto(version)
            resultado = _texto(resultado)
            commit = _texto(commit)

            if modelo:
                datos["modelo"] = modelo
            if version:
                datos["version"] = version
            if resultado:
                datos["resultado"] = resultado
            if commit:
                # Un ciclo sin cambios no genera commit: se conserva el del
                # ultimo cambio real, que es lo que el panel quiere enlazar.
                datos["ultimo_commit"] = commit

            datos["ultimo_intento"] = momento
            if ok:
                datos["ultimo_ok"] = momento

            equipos[nombre] = datos
            self._guardar(equipos)

    def limpiar(self, nombres) -> int:
        """Olvida los equipos que ya no estan en el inventario. Devuelve cuantos.

        Quien llama tiene que pasar el inventario COMPLETO, no el subconjunto que
        se acaba de respaldar: con el planificador un ciclo toca solo a los
        equipos a los que les toca, y limpiar con esa lista borraria lo que se
        sabe de toda la flota en cada vuelta.
        """
        vivos = {_texto(getattr(n, "nombre", n)) for n in (nombres or ())}
        vivos.discard("")

        if not vivos:
            # Una lista vacia casi siempre significa "no se pudo saber cual es
            # el inventario", no "ya no hay equipos". Borrarlo todo es
            # irrecuperable y el coste de no hacerlo es dejar unas filas viejas.
            log.debug("limpiar() sin nombres: no se borra nada")
            return 0

        with self._lock:
            equipos = self._cargar()
            sobran = [n for n in equipos if n not in vivos]
            if not sobran:
                return 0
            for n in sobran:
                del equipos[n]
            self._guardar(equipos)
            return len(sobran)

    # --- Lectura (la usa el panel) ------------------------------------------

    def leer(self) -> dict[str, dict]:
        """Todo lo que se sabe: {nombre: {campos}}. NUNCA lanza.

        Si el archivo no existe, esta a medias o corrupto, se devuelve un dict
        vacio: el panel dira "no se sabe" y seguira pintando el resto. Que este
        archivo este roto no puede tumbar ni el panel ni un respaldo.
        """
        with self._lock:
            return {n: dict(d) for n, d in self._cargar().items()}

    def de(self, nombre: str) -> dict:
        """Lo que se sabe de UN equipo, o {} si no se sabe nada. NUNCA lanza."""
        nombre = _texto(nombre)
        if not nombre:
            return {}
        with self._lock:
            return dict(self._cargar().get(nombre) or {})

    # --- Interior -----------------------------------------------------------

    def _cargar(self, intentos: int = 12) -> dict[str, dict]:
        """Lee el archivo. Debe llamarse con el lock tomado. Nunca lanza.

        Los reintentos son por Windows, igual que en estado.leer(): alli
        os.replace bloquea el archivo un instante y un lector que caiga en esa
        ventana recibe PermissionError. En Linux nunca se usan.
        """
        espera = 0.005
        crudo = None
        for intento in range(intentos):
            try:
                crudo = self.ruta.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Todavia no ha corrido nunca: se empieza de cero, sin ruido en
                # el log (es lo normal en la primera ejecucion).
                return {}
            except OSError as exc:
                if intento == intentos - 1:
                    log.warning(
                        "No se pudo leer %s (%s): se sigue con lo ultimo conocido",
                        self.ruta, exc,
                    )
                    # Lo ultimo bueno, no {}: ver el comentario de _ultimo.
                    return {n: dict(d) for n, d in self._ultimo.items()}
                time.sleep(espera)
                espera = min(espera * 1.7, 0.05)
            else:
                break

        try:
            datos = json.loads(crudo or "")
        except ValueError:
            log.warning(
                "%s no es JSON valido: se empieza de cero (se reconstruye solo "
                "segun vayan respondiendo los equipos)", self.ruta,
            )
            return {}

        if not isinstance(datos, dict):
            log.warning("%s no tiene la forma esperada: se empieza de cero", self.ruta)
            return {}

        equipos = datos.get("equipos")
        if not isinstance(equipos, dict):
            return {}

        # Se filtra lo que no encaje en vez de confiar: este archivo se puede
        # editar a mano y el panel lo pinta tal cual.
        limpios: dict[str, dict] = {}
        for nombre, campos in equipos.items():
            if not isinstance(nombre, str) or not isinstance(campos, dict):
                continue
            limpios[nombre] = {
                c: _texto(campos[c]) for c in CAMPOS if isinstance(campos.get(c), str)
            }

        self._ultimo = {n: dict(d) for n, d in limpios.items()}
        return limpios

    def _guardar(self, equipos: dict[str, dict]) -> None:
        """Vuelca el archivo. Con el lock tomado. Nunca lanza.

        Escritura atomica (temporal en la misma carpeta + os.replace): el panel
        lee este archivo mientras el respaldo lo escribe y no puede encontrarse
        nunca un JSON a medias. El temporal va al lado del definitivo porque
        os.replace solo es atomico dentro del mismo sistema de archivos.
        """
        contenido = {
            "formato": FORMATO,
            "actualizado": _ahora(),
            # Ordenado por nombre: asi dos volcados seguidos se pueden comparar
            # con un diff y el archivo se lee a ojo sin buscar.
            "equipos": {n: equipos[n] for n in sorted(equipos)},
        }

        temporal = ""
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.ruta.parent,
                prefix=self.ruta.name + ".",
                suffix=".tmp",
                delete=False,
            ) as fh:
                json.dump(contenido, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
                temporal = fh.name

            # Sobre el TEMPORAL y ANTES del replace, como en inventory.guardar:
            # si se hiciera despues, el archivo definitivo existiria un instante
            # con los permisos por defecto (0644 con el umask habitual). Aqui no
            # hay credenciales, pero si el inventario completo de la flota con su
            # modelo y su version de RouterOS, que es justo el mapa que busca
            # quien quiere saber a que equipo le falta un parche.
            try:
                os.chmod(temporal, 0o600)
            except OSError:
                # En Windows chmod es casi simbolico y puede fallar segun el
                # sistema de archivos. No es motivo para perder la anotacion:
                # donde esto importa de verdad, el Debian del servicio, funciona.
                pass

            os.replace(temporal, self.ruta)
        except OSError as exc:
            log.warning("No se pudo escribir %s: %s", self.ruta, exc)
            if temporal:
                try:
                    os.unlink(temporal)
                except OSError:
                    pass
            return

        self._ultimo = {n: dict(d) for n, d in equipos.items()}
