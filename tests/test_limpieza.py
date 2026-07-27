"""Pruebas de la normalizacion del export y del inventario.

La limpieza es la pieza mas critica: si limpia de menos, cada respaldo parece
un cambio y el repo se llena de commits vacios. Si limpia de mas, se pierde
configuracion real sin que nadie se entere.

La segunda mitad prueba el inventario:

  - el intervalo por equipo: una columna opcional que decide cada cuanto se
    consulta cada router y que, mal leida, o deja un equipo sin respaldar o lo
    machaca a sesiones SSH.
  - las credenciales por equipo (usuario/clave): opcionales, se heredan de
    config.yaml si faltan, y la clave NO se recorta nunca.
  - la ruta dentro del repo, que ya no lleva el grupo.

Ejecutar:  python -m tests.test_limpieza
"""

import os
import stat
import tempfile
from pathlib import Path

from mkbackup.device import limpiar_export
from mkbackup.inventory import CABECERA, Equipo, cargar, guardar, validar_equipo

FALLOS = []


def comprobar(descripcion: str, condicion: bool) -> None:
    estado = "OK  " if condicion else "FALLA"
    print(f"[{estado}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


BASE = """# 2026-07-25 10:00:00 by RouterOS 7.14
# software id = ABCD-1234
#
/interface bridge
add name=bridge1 protocol-mode=rstp
/ip dns
set servers=8.8.8.8
"""


def test_fecha_v7():
    otro = BASE.replace("10:00:00", "16:30:45")
    comprobar(
        "distinta hora (v7) no cuenta como cambio",
        limpiar_export(BASE) == limpiar_export(otro),
    )


def test_fecha_v6():
    v6a = "# jul/25/2026 10:00:00 by RouterOS 6.49.10\n/ip dns\nset servers=8.8.8.8\n"
    v6b = "# jul/26/2026 03:11:59 by RouterOS 6.49.10\n/ip dns\nset servers=8.8.8.8\n"
    comprobar("distinta fecha (v6) no cuenta como cambio",
              limpiar_export(v6a) == limpiar_export(v6b))


def test_ruido_intermitente():
    con_ruido = BASE.replace(
        "/ip dns",
        "# inactive time\n# poe-out status: short_circuit\n# ether5 not ready\n/ip dns",
    )
    comprobar(
        "comentarios intermitentes no cuentan como cambio",
        limpiar_export(BASE) == limpiar_export(con_ruido),
    )


def test_cambio_real():
    cambiado = BASE.replace("8.8.8.8", "1.1.1.1")
    comprobar(
        "un cambio real SI se detecta",
        limpiar_export(BASE) != limpiar_export(cambiado),
    )


def test_union_lineas_partidas():
    # RouterOS parte los comandos largos con backslash + salto + sangria.
    partido = (
        "/ip address\n"
        "add address=192.168.1.1/24 interface=bridge1 \\\n"
        "    network=192.168.1.0\n"
    )
    salida = limpiar_export(partido)
    comprobar("la linea partida se une", "\\" not in salida)
    comprobar(
        "al unir no quedan espacios de mas",
        "add address=192.168.1.1/24 interface=bridge1 network=192.168.1.0" in salida,
    )


def test_crlf():
    # RouterOS entrega CRLF; si no se normaliza, todo el archivo parece cambiado.
    comprobar(
        "CRLF y LF producen el mismo resultado",
        limpiar_export(BASE.replace("\n", "\r\n")) == limpiar_export(BASE),
    )


def test_no_come_configuracion():
    salida = limpiar_export(BASE)
    comprobar("conserva las lineas de configuracion",
              "add name=bridge1 protocol-mode=rstp" in salida
              and "set servers=8.8.8.8" in salida)
    comprobar("conserva el software id", "software id" in salida)


def test_termina_en_salto():
    comprobar("termina siempre en un unico salto de linea",
              limpiar_export(BASE).endswith("\n")
              and not limpiar_export(BASE).endswith("\n\n"))


# --- Inventario: intervalo por equipo ---------------------------------------


def _cargar_texto(texto: str):
    """Escribe un inventario de prueba y lo carga. Devuelve (equipos, avisos)."""
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "inventory.csv"
        ruta.write_text(texto, encoding="utf-8", newline="")
        return cargar(ruta)


def test_intervalo_columna_ausente():
    # Los inventarios escritos antes de que la columna existiera tienen que
    # seguir cargando: sus equipos se consultan al ritmo global.
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo\n"
        "R1,Andinanet,10.0.0.1,22,core\n"
        "R2,Andinanet,10.0.0.2,22,core\n"
    )
    comprobar("sin columna intervalo: el inventario sigue cargando",
              len(equipos) == 2)
    comprobar("sin columna intervalo: todos heredan el global (0)",
              all(e.intervalo_minutos == 0 for e in equipos))
    comprobar("sin columna intervalo: no genera avisos", avisos == [])


def test_intervalo_vacio_y_valido():
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos\n"
        "R-Hereda,Andinanet,10.0.0.1,22,core,\n"
        "R-Diario,Andinanet,10.0.0.2,22,core,1440\n"
    )
    comprobar("intervalo vacio: queda en 0 (hereda el global)",
              equipos[0].intervalo_minutos == 0)
    comprobar("intervalo valido: se lee tal cual",
              equipos[1].intervalo_minutos == 1440)
    comprobar("intervalo vacio o valido: no genera avisos", avisos == [])


def test_intervalo_no_numerico():
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos\n"
        "R-Raro,Andinanet,10.0.0.1,22,core,cada hora\n"
    )
    comprobar("intervalo no numerico: NO descarta el equipo", len(equipos) == 1)
    comprobar("intervalo no numerico: se cae al global (0)",
              equipos[0].intervalo_minutos == 0)
    comprobar("intervalo no numerico: avisa y nombra el valor",
              any("cada hora" in a for a in avisos))


def test_intervalo_negativo():
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos\n"
        "R-Negativo,Andinanet,10.0.0.1,22,core,-30\n"
    )
    comprobar("intervalo negativo: NO descarta el equipo", len(equipos) == 1)
    comprobar("intervalo negativo: se cae al global (0)",
              equipos[0].intervalo_minutos == 0)
    comprobar("intervalo negativo: avisa",
              any("negativo" in a for a in avisos))


def test_intervalo_agresivo():
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos\n"
        "R-Nervioso,Andinanet,10.0.0.1,22,core,2\n"
        "R-Cero,Andinanet,10.0.0.2,22,core,0\n"
        "R-Tranquilo,Andinanet,10.0.0.3,22,core,60\n"
    )
    comprobar("intervalo agresivo: se acepta igual, no se descarta",
              [e.intervalo_minutos for e in equipos] == [2, 0, 60])
    agresivos = [a for a in avisos if "SSH" in a]
    comprobar("intervalo agresivo: avisa una vez y solo del equipo nervioso",
              len(agresivos) == 1 and "R-Nervioso" in agresivos[0])
    comprobar("intervalo 0 explicito: no se confunde con uno agresivo",
              not any("R-Cero" in a for a in avisos))


def test_intervalo_ida_y_vuelta():
    # Lo que el panel guarda tiene que volver igual: si el ciclo perdiera el
    # intervalo, editar cualquier campo de un equipo lo devolveria al global.
    equipos = [
        Equipo("R-Hereda", "10.0.0.1", 22, "core", "Andinanet"),
        Equipo("R-Diario", "10.0.0.2", 2222, "bts", "Andinanet", 1440),
    ]
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "inventory.csv"
        guardar(ruta, equipos)
        cabecera = ruta.read_text(encoding="utf-8").splitlines()[0]
        comprobar("guardar: escribe la cabecera completa",
                  cabecera == ",".join(CABECERA))
        comprobar("guardar: la cabecera incluye intervalo_minutos",
                  "intervalo_minutos" in cabecera.split(","))
        vueltos, avisos = cargar(ruta)

    comprobar("cargar -> guardar -> cargar: conserva los equipos",
              vueltos == equipos)
    comprobar("cargar -> guardar -> cargar: conserva el intervalo",
              [e.intervalo_minutos for e in vueltos] == [0, 1440])
    comprobar("cargar -> guardar -> cargar: sin avisos", avisos == [])


def test_validar_equipo_intervalo():
    def validar(intervalo):
        return validar_equipo(
            "R1", "Andinanet", "10.0.0.1", "22", "core", existentes=[],
            intervalo=intervalo,
        )

    # Vacio, None y 0 son lo mismo: usa el intervalo global. No son errores.
    for etiqueta, valor in (("vacio", ""), ("None", None), ("cero", "0")):
        equipo, errores = validar(valor)
        comprobar(f"validar_equipo: intervalo {etiqueta} se acepta ({errores})",
                  equipo is not None and equipo.intervalo_minutos == 0)

    equipo, errores = validar("1440")
    comprobar(f"validar_equipo: intervalo valido se guarda ({errores})",
              equipo is not None and equipo.intervalo_minutos == 1440)

    equipo, errores = validar(720)
    comprobar("validar_equipo: acepta el intervalo como entero, no solo texto",
              equipo is not None and equipo.intervalo_minutos == 720)

    equipo, errores = validar("cada hora")
    comprobar("validar_equipo: intervalo no numerico es error",
              equipo is None and any("numero" in e for e in errores))

    equipo, errores = validar("-5")
    comprobar("validar_equipo: intervalo negativo es error",
              equipo is None and any("negativo" in e for e in errores))

    # Un intervalo agresivo NO se rechaza en el formulario: es su red. El aviso
    # lo da cargar() al leer el inventario.
    equipo, errores = validar("2")
    comprobar(f"validar_equipo: un intervalo bajo se acepta ({errores})",
              equipo is not None and equipo.intervalo_minutos == 2)


# --- Inventario: credenciales por equipo ------------------------------------


def test_credenciales_columnas_ausentes():
    # Un inventario de antes de que existieran las columnas: todos los equipos
    # tienen que quedar heredando las credenciales generales, sin ruido.
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos\n"
        "R1,Andinanet,10.0.0.1,22,core,\n"
        "R2,Andinanet,10.0.0.2,22,core,\n"
    )
    comprobar("sin columnas usuario/clave: el inventario sigue cargando",
              len(equipos) == 2)
    comprobar("sin columnas usuario/clave: quedan vacias (heredan las generales)",
              all(e.usuario == "" and e.clave == "" for e in equipos))
    comprobar("sin columnas usuario/clave: no genera avisos", avisos == [])


def test_credenciales_vacias_y_completas():
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave\n"
        "R-Hereda,Andinanet,10.0.0.1,22,core,,,\n"
        "R-Propias,Andinanet,10.0.0.2,22,core,,soporte,s3cr3ta\n"
    )
    comprobar("credenciales vacias: quedan vacias, no se inventan",
              equipos[0].usuario == "" and equipos[0].clave == "")
    comprobar("credenciales propias: se leen las dos",
              equipos[1].usuario == "soporte" and equipos[1].clave == "s3cr3ta")
    comprobar("credenciales vacias o completas: no generan avisos", avisos == [])


def test_credenciales_solo_usuario():
    # Se mezclaria el usuario del equipo con la clave general: casi siempre es
    # un descuido al llenar el Excel, pero la fila es valida y no se descarta.
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave\n"
        "R-Manco,Andinanet,10.0.0.1,22,core,,soporte,\n"
    )
    comprobar("solo usuario: NO descarta el equipo", len(equipos) == 1)
    comprobar("solo usuario: se conserva tal cual",
              equipos[0].usuario == "soporte" and equipos[0].clave == "")
    comprobar("solo usuario: avisa de la mezcla con la clave general",
              any("R-Manco" in a and "clave general" in a for a in avisos))


def test_credenciales_solo_clave():
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave\n"
        "R-Coja,Andinanet,10.0.0.1,22,core,,,s3cr3ta\n"
    )
    comprobar("solo clave: NO descarta el equipo", len(equipos) == 1)
    comprobar("solo clave: se conserva tal cual",
              equipos[0].usuario == "" and equipos[0].clave == "s3cr3ta")
    comprobar("solo clave: avisa de la mezcla con el usuario general",
              any("R-Coja" in a and "usuario general" in a for a in avisos))


def test_credenciales_espacios():
    # El usuario con espacios es un copiar y pegar mal: se recorta. La clave NO
    # se toca jamas: ese espacio final puede ser parte de la contrasena, y
    # quitarlo daria un fallo de autenticacion imposible de diagnosticar.
    equipos, avisos = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave\n"
        'R-Pegado,Andinanet,10.0.0.1,22,core,,"  soporte  ","s3cr3ta "\n'
    )
    comprobar("usuario con espacios: SI se recortan",
              equipos[0].usuario == "soporte")
    comprobar("clave con espacio final: NO se recorta",
              equipos[0].clave == "s3cr3ta ")
    comprobar("usuario y clave presentes: no avisa de mezcla", avisos == [])


def test_credenciales_ida_y_vuelta():
    # Si el ciclo perdiera la clave, editar cualquier campo del equipo en el
    # panel lo dejaria autenticando con la credencial general y fallando.
    equipos = [
        Equipo("R-Hereda", "10.0.0.1", 22, "core", "Andinanet"),
        Equipo("R-Propias", "10.0.0.2", 2222, "bts", "Andinanet", 1440,
               "soporte", "s3cr3ta con espacio final "),
    ]
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "inventory.csv"
        guardar(ruta, equipos)
        cabecera = ruta.read_text(encoding="utf-8").splitlines()[0]
        comprobar("guardar: escribe las columnas usuario y clave",
                  cabecera.endswith("usuario,clave"))
        vueltos, avisos = cargar(ruta)

    comprobar("cargar -> guardar -> cargar: conserva los equipos enteros",
              vueltos == equipos)
    comprobar("cargar -> guardar -> cargar: conserva usuario y clave",
              vueltos[1].usuario == "soporte"
              and vueltos[1].clave == "s3cr3ta con espacio final ")
    comprobar("cargar -> guardar -> cargar: el que hereda sigue heredando",
              vueltos[0].usuario == "" and vueltos[0].clave == "")
    comprobar("cargar -> guardar -> cargar: sin avisos", avisos == [])


def test_guardar_deja_permisos_restrictivos():
    # El inventario puede llevar contrasenas en claro: en el Debian del
    # servicio tiene que quedar en 0600. En Windows chmod es casi simbolico,
    # asi que alli solo se comprueba que no reviente.
    equipos = [Equipo("R1", "10.0.0.1", 22, "core", "Andinanet", 0,
                      "soporte", "s3cr3ta")]
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "inventory.csv"
        guardar(ruta, equipos)
        comprobar("guardar: con credenciales escribe el archivo igual",
                  ruta.is_file())
        if os.name == "nt":
            comprobar("guardar: en Windows el chmod no revienta el alta",
                      cargar(ruta)[0][0].clave == "s3cr3ta")
        else:
            modo = stat.S_IMODE(ruta.stat().st_mode)
            comprobar(f"guardar: el inventario queda en 0600 (es {modo:o})",
                      modo == 0o600)


def test_validar_equipo_credenciales():
    def validar(usuario="", clave=""):
        return validar_equipo(
            "R1", "Andinanet", "10.0.0.1", "22", "core", existentes=[],
            usuario=usuario, clave=clave,
        )

    # Vacias es lo normal: hereda las generales. No es error.
    equipo, errores = validar()
    comprobar(f"validar_equipo: sin credenciales se acepta ({errores})",
              equipo is not None and equipo.usuario == "" and equipo.clave == "")

    equipo, errores = validar("soporte", "s3cr3ta")
    comprobar(f"validar_equipo: credenciales propias se guardan ({errores})",
              equipo is not None and equipo.usuario == "soporte"
              and equipo.clave == "s3cr3ta")

    # Llenar solo una no se rechaza: puede ser a proposito (usuario propio con
    # la clave de siempre). El aviso de la mezcla lo da cargar().
    equipo, errores = validar("soporte", "")
    comprobar(f"validar_equipo: solo usuario no es error ({errores})",
              equipo is not None and equipo.usuario == "soporte")
    equipo, errores = validar("", "s3cr3ta")
    comprobar(f"validar_equipo: solo clave no es error ({errores})",
              equipo is not None and equipo.clave == "s3cr3ta")

    equipo, errores = validar("  soporte  ", "  s3cr3ta  ")
    comprobar("validar_equipo: recorta el usuario",
              equipo is not None and equipo.usuario == "soporte")
    comprobar("validar_equipo: NO recorta la clave",
              equipo is not None and equipo.clave == "  s3cr3ta  ")


# --- Inventario: ruta dentro del repo ---------------------------------------


def test_ruta_relativa_sin_grupo():
    # El grupo ya NO entra en la ruta: el usuario quiere la carpeta de cada
    # empresa con todos sus respaldos dentro, sin subcarpetas por grupo.
    equipo = Equipo("BTS-Norte-01", "10.0.0.1", 22, "bts", "Andinanet S.A.")
    comprobar("ruta relativa: es <empresa>/<nombre>.rsc",
              equipo.ruta_relativa == "andinanet-s.a/BTS-Norte-01.rsc")
    comprobar("ruta relativa: el grupo no aparece",
              "bts" not in equipo.ruta_relativa)

    # Reclasificar un equipo no debe mover su archivo ni partir su historial.
    reclasificado = Equipo("BTS-Norte-01", "10.0.0.1", 22, "core", "Andinanet S.A.")
    comprobar("ruta relativa: cambiar de grupo no mueve el archivo",
              reclasificado.ruta_relativa == equipo.ruta_relativa)


def test_ruta_relativa_mismo_nombre_dos_empresas():
    # Dos clientes con un 'Router-Principal' cada uno son lo mas normal del
    # mundo: la empresa tiene que seguir separandolos.
    uno = Equipo("Router-Principal", "10.1.0.1", 22, "core", "Andinanet")
    dos = Equipo("Router-Principal", "10.2.0.1", 22, "core", "Fibra Austral")
    comprobar("ruta relativa: el mismo nombre en dos empresas no choca",
              uno.ruta_relativa != dos.ruta_relativa)
    comprobar("ruta relativa: cada uno cae en la carpeta de su empresa",
              uno.ruta_relativa == "andinanet/Router-Principal.rsc"
              and dos.ruta_relativa == "fibra-austral/Router-Principal.rsc")


def test_grupo_sigue_en_el_inventario():
    # Aunque no vaya en la ruta, el grupo se sigue leyendo y escribiendo: el
    # panel filtra por el.
    equipos, _ = _cargar_texto(
        "nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave\n"
        "R-Core,Andinanet,10.0.0.1,22,core,,,\n"
        "R-Bts,Andinanet,10.0.0.2,22,bts,,,\n"
    )
    comprobar("el grupo se sigue leyendo del CSV",
              [e.grupo for e in equipos] == ["core", "bts"])
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "inventory.csv"
        guardar(ruta, equipos)
        texto = ruta.read_text(encoding="utf-8")
    comprobar("el grupo se sigue escribiendo en el CSV",
              "grupo" in texto.splitlines()[0] and ",bts," in texto)


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_") and callable(funcion):
            funcion()

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallidas:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")
