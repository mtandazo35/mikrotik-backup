"""Prueba de las cuentas del panel: permisos, alcance, roles y el archivo.

El panel gestiona routers de varias empresas cliente de un ISP, asi que aqui se
comprueban dos cosas distintas y que no se pueden confundir:

  - QUE puede hacer una cuenta: permisos efectivos = (rol | mas) - menos, con
    `menos` ganando siempre y los permisos desconocidos ignorados
  - SOBRE QUE equipos: el alcance (todo / por empresa / por equipo suelto)

Y ademas:

  - los roles son datos: se crean, se editan y se borran, pero no se borra uno
    en uso ni se borra 'admin'
  - LA INVARIANTE: nunca puede quedarse el sistema sin ningun usuario que tenga
    a la vez el permiso 'usuarios' y alcance a todas las empresas. Ese es el
    unico que puede volver a repartirlo todo, y se protege por sus cuatro
    caminos directos (borrar, cambiar rol, quitar permiso, recortar alcance) y
    por los dos indirectos (editar o borrar el rol que lo sostiene)
  - las contrasenas no se guardan en claro y la vieja deja de valer al cambiarla
  - un archivo corrupto no autentica a nadie ni se sobreescribe en silencio
  - la cuenta de config.yaml se migra sola y solo una vez
  - un archivo de formato 1 se migra a formato 2 sin perder nada y sin cambiarle
    los permisos a nadie por sorpresa
  - dos procesos (el programador y el panel) ven los mismos usuarios

No levanta el servidor ni toca la red.

Ejecutar:  python -m tests.test_usuarios
"""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from mkbackup import sesion as ses
from mkbackup import usuarios as usr

FALLOS = []

# Coste de pbkdf2 en las pruebas. Las 240.000 iteraciones de produccion son
# medio segundo por hash, y aqui se crean y se verifican decenas de claves:
# la prueba tardaria minutos. Lo que se prueba es la logica de las cuentas, no
# lo caro que sale el hash (de eso se ocupa test_sesion), asi que basta 1000.
ITER = 1000


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def nuevo(carpeta: Path, nombre: str = "usuarios.json", iteraciones: int = ITER):
    """Un almacen limpio sobre un archivo que todavia no existe."""
    ruta = carpeta / nombre
    if ruta.exists():
        ruta.unlink()
    return usr.Usuarios(ruta, iteraciones=iteraciones), ruta


def main() -> None:
    temporal = Path(tempfile.mkdtemp(prefix="mkbackup-usuarios-"))
    try:
        pruebas(temporal)
    finally:
        shutil.rmtree(temporal, ignore_errors=True)

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


def pruebas(carpeta: Path) -> None:
    # 1. Archivo que todavia no existe: no es un error, es que no hay cuentas.
    u, ruta = nuevo(carpeta)
    comprobar("sin archivo, listar devuelve vacio", u.listar() == [])
    comprobar("sin archivo, obtener devuelve None", u.obtener("admin") is None)
    comprobar("sin archivo, no autentica a nadie",
              u.autenticar("admin", "loquesea") is None)
    comprobar("consultar no crea el archivo por su cuenta", not ruta.exists())
    comprobar("sin archivo ya hay roles: los iniciales",
              u.roles() == {k: list(v) for k, v in usr.ROLES_INICIALES.items()})

    # 2. Alta de la primera cuenta.
    comprobar("crear un admin no da errores",
              u.crear("admin", "clave-larga-1", "admin") == [])
    comprobar("el archivo aparece al crear la primera cuenta", ruta.exists())
    lista = u.listar()
    comprobar("ahora hay una cuenta", len(lista) == 1)
    cuenta = lista[0]
    comprobar("con el nombre y el rol que se pidieron",
              cuenta.nombre == "admin" and cuenta.rol == "admin")
    comprobar("se apunta la fecha de creacion", bool(cuenta.creado))
    comprobar("y todavia no ha entrado nunca", cuenta.ultimo_acceso == "")
    # La PRIMERA cuenta se lleva las llaves: si no, un panel recien instalado
    # nace sin nadie que pueda repartir acceso a ninguna empresa.
    comprobar("la primera cuenta nace con alcance a todo", cuenta.alcance.todo)
    comprobar("y sin excepciones de permisos",
              cuenta.permisos_mas == [] and cuenta.permisos_menos == [])

    crudo = ruta.read_text(encoding="utf-8")
    comprobar("la contrasena NO se guarda en claro", "clave-larga-1" not in crudo)
    comprobar("lo que se guarda es un hash pbkdf2",
              ses.formato_valido(json.loads(crudo)["usuarios"][0]["clave_hash"]))
    comprobar("el archivo declara la version del formato",
              json.loads(crudo).get("formato") == usr.FORMATO)
    comprobar("y guarda los roles como datos, en el mismo archivo",
              json.loads(crudo).get("roles", {}).get("admin") == list(usr.PERMISOS))

    # 3. obtener no distingue mayusculas (autenticar tampoco, ver mas abajo).
    comprobar("obtener encuentra la cuenta", u.obtener("admin") is not None)
    comprobar("obtener no distingue mayusculas", u.obtener("ADMIN") is not None)
    comprobar("obtener de quien no existe devuelve None",
              u.obtener("fantasma") is None)
    prestada = u.obtener("admin")
    prestada.rol = "lector"
    prestada.permisos_menos.append("usuarios")
    prestada.alcance.todo = False
    prestada.alcance.empresas["Acme"] = usr.VER
    devuelta = u.obtener("admin")
    comprobar("cambiar el objeto devuelto no toca el almacen",
              devuelta.rol == "admin"
              and devuelta.permisos_menos == []
              and devuelta.alcance.todo
              and devuelta.alcance.empresas == {})

    # 4. Validaciones del alta. Ninguna debe dejar rastro en el archivo.
    antes = len(u.listar())
    casos = [
        ("nombre vacio", ("", "clave-larga-1", "lector")),
        ("nombre con espacios", ("juan perez", "clave-larga-1", "lector")),
        ("nombre demasiado corto", ("ab", "clave-larga-1", "lector")),
        ("nombre demasiado largo", ("x" * 33, "clave-larga-1", "lector")),
        ("nombre con caracteres raros", ("juan@casa", "clave-larga-1", "lector")),
        # Colado entre los rechazos a proposito: si la validacion se pasara de
        # dura, este de aqui tambien caeria y nadie se enteraria.
        ("nombre corriente", ("jose", "clave-larga-1", "lector")),
        ("clave demasiado corta", ("pepe", "1234567", "lector")),
        ("clave vacia", ("pepe", "", "lector")),
        ("rol inventado", ("pepe", "clave-larga-1", "superadmin")),
        ("rol vacio", ("pepe", "clave-larga-1", "")),
        ("rol con la etiqueta en vez del valor",
         ("pepe", "clave-larga-1", "Administrador")),
    ]
    for descripcion, (n, c, r) in casos:
        errores = u.crear(n, c, r)
        if descripcion == "nombre corriente":
            comprobar("un nombre normal de 4 letras si se acepta", errores == [])
            continue
        comprobar(f"se rechaza: {descripcion}", errores != [])
        comprobar(f"y el error viene explicado: {descripcion}",
                  len(errores) >= 1 and all(isinstance(e, str) and e for e in errores))

    comprobar("de todo eso solo entro la cuenta valida",
              len(u.listar()) == antes + 1)
    comprobar("el rechazado no se colo", u.obtener("pepe") is None)

    # La segunda cuenta NO hereda las llaves: en un panel multitenant el fallo
    # por omision no puede ser "ve los routers de todos los clientes".
    comprobar("una cuenta que no es la primera nace sin alcance",
              not u.obtener("jose").alcance.todo
              and u.obtener("jose").alcance.empresas == {})

    comprobar("un permiso inventado en permisos_mas se rechaza",
              u.crear("raro1", "clave-larga-1", "lector",
                      permisos_mas=["volar"]) != [])
    comprobar("y en permisos_menos tambien",
              u.crear("raro2", "clave-larga-1", "lector",
                      permisos_menos=["volar"]) != [])

    # La longitud minima es exacta: 8 vale, 7 no.
    comprobar("una clave de 7 caracteres no vale",
              u.crear("clavecorta", "1234567", "lector") != [])
    comprobar(f"una de {usr.MINIMO_CLAVE} si vale",
              u.crear("clavejusta", "1" * usr.MINIMO_CLAVE, "lector") == [])

    # 5. Duplicados: se comparan sin distinguir mayusculas, pero el nombre se
    #    guarda tal como se escribio.
    comprobar("no se puede repetir el nombre", u.crear("admin", "otra-clave-1", "admin") != [])
    comprobar("ni cambiando las mayusculas", u.crear("ADMIN", "otra-clave-1", "admin") != [])
    comprobar("ni mezclandolas", u.crear("AdMiN", "otra-clave-1", "admin") != [])
    comprobar("el mensaje de duplicado nombra la cuenta que ya existe",
              "admin" in u.crear("Admin", "otra-clave-1", "admin")[0])
    comprobar("crear con mayusculas propias se guarda tal cual",
              u.crear("Soporte.Nivel_2", "clave-larga-1", "operador") == []
              and u.obtener("soporte.nivel_2").nombre == "Soporte.Nivel_2")

    # 6. Orden alfabetico, sin que las mayusculas manden.
    nombres = [x.nombre for x in u.listar()]
    comprobar("la lista sale ordenada por nombre",
              nombres == sorted(nombres, key=str.lower))

    # 7. Autenticar.
    comprobar("la clave correcta entra",
              (u.autenticar("admin", "clave-larga-1") or None) is not None)
    comprobar("y devuelve la cuenta con su rol",
              u.autenticar("admin", "clave-larga-1").rol == "admin")
    comprobar("el nombre no distingue mayusculas al entrar",
              u.autenticar("ADMIN", "clave-larga-1") is not None)
    comprobar("la clave SI distingue mayusculas",
              u.autenticar("admin", "CLAVE-LARGA-1") is None)
    comprobar("una clave equivocada no entra",
              u.autenticar("admin", "clave-larga-2") is None)
    comprobar("la clave vacia no entra", u.autenticar("admin", "") is None)
    comprobar("un usuario que no existe no entra",
              u.autenticar("fantasma", "clave-larga-1") is None)
    comprobar("el nombre vacio no entra", u.autenticar("", "clave-larga-1") is None)

    # El ultimo acceso queda apuntado y sobrevive al proceso: quien mire el
    # panel manana tiene que ver quien entro hoy.
    comprobar("al entrar se apunta el ultimo acceso",
              bool(u.obtener("admin").ultimo_acceso))
    otro_proceso = usr.Usuarios(ruta, iteraciones=ITER)
    comprobar("y queda guardado en el archivo, no solo en memoria",
              bool(otro_proceso.obtener("admin").ultimo_acceso))
    comprobar("a quien no ha entrado no se le inventa un acceso",
              otro_proceso.obtener("clavejusta").ultimo_acceso == "")

    # 8. La clave puede llevar espacios y no se recortan: son parte de ella.
    con_espacios = "  clave con espacios  "
    comprobar("se acepta una clave con espacios al principio y al final",
              u.crear("espacios", con_espacios, "lector") == [])
    comprobar("y entra tal cual se escribio",
              u.autenticar("espacios", con_espacios) is not None)
    comprobar("la version recortada NO entra",
              u.autenticar("espacios", con_espacios.strip()) is None)

    # 9. Cambiar la clave: la vieja deja de valer en el acto.
    comprobar("cambiar la clave no da errores",
              u.cambiar_clave("espacios", "clave-nueva-9") == [])
    comprobar("la clave nueva entra",
              u.autenticar("espacios", "clave-nueva-9") is not None)
    comprobar("y la vieja ya no vale",
              u.autenticar("espacios", con_espacios) is None)
    comprobar("no se puede poner una clave demasiado corta",
              u.cambiar_clave("espacios", "corta") != [])
    comprobar("y la que habia sigue valiendo despues del intento fallido",
              u.autenticar("espacios", "clave-nueva-9") is not None)
    comprobar("no se puede cambiar la clave de quien no existe",
              u.cambiar_clave("fantasma", "clave-larga-1") != [])
    comprobar("cambiar la clave no cambia el rol",
              u.obtener("espacios").rol == "lector")

    # 10. Cambiar el rol (y el resto de actualizar).
    comprobar("cambiar el rol no da errores",
              u.cambiar_rol("espacios", "operador") == [])
    comprobar("el rol nuevo queda guardado", u.obtener("espacios").rol == "operador")
    comprobar("no se acepta un rol inventado",
              u.cambiar_rol("espacios", "jefe") != [])
    comprobar("y el rol anterior no se toca tras el intento fallido",
              u.obtener("espacios").rol == "operador")
    comprobar("no se puede cambiar el rol de quien no existe",
              u.cambiar_rol("fantasma", "lector") != [])
    comprobar("cambiar el rol no invalida la clave",
              u.autenticar("espacios", "clave-nueva-9") is not None)

    comprobar("actualizar sin argumentos no cambia nada",
              u.actualizar("espacios") == []
              and u.obtener("espacios").rol == "operador")
    comprobar("actualizar solo el alcance no toca el rol",
              u.actualizar("espacios",
                           alcance=usr.Alcance(empresas={"Acme": usr.VER})) == []
              and u.obtener("espacios").rol == "operador"
              and u.obtener("espacios").alcance.puede_ver("Acme", "core-01"))
    comprobar("actualizar acepta el alcance como diccionario",
              u.actualizar("espacios",
                           alcance={"todo": False,
                                    "empresas": {"Bravo": usr.EDITAR}}) == []
              and u.obtener("espacios").alcance.puede_editar("Bravo", "rb1"))
    comprobar("y lo anterior se reemplaza, no se acumula",
              not u.obtener("espacios").alcance.puede_ver("Acme", "core-01"))
    comprobar("actualizar rechaza un permiso inventado",
              u.actualizar("espacios", permisos_mas=["volar"]) != [])
    comprobar("actualizar avisa si la cuenta no existe",
              u.actualizar("fantasma", rol="lector") != [])

    # 11. Borrar.
    comprobar("borrar no da errores", u.borrar("clavejusta") == [])
    comprobar("la cuenta borrada desaparece", u.obtener("clavejusta") is None)
    comprobar("y ya no puede entrar",
              u.autenticar("clavejusta", "1" * usr.MINIMO_CLAVE) is None)
    comprobar("borrar dos veces avisa de que no existe", u.borrar("clavejusta") != [])
    comprobar("borrar a quien no existe avisa", u.borrar("fantasma") != [])
    comprobar("borrar no distingue mayusculas",
              u.crear("temporal", "clave-larga-1", "lector") == []
              and u.borrar("TEMPORAL") == []
              and u.obtener("temporal") is None)

    # 12. Permisos efectivos: (rol | mas) - menos, y lo desconocido se ignora.
    perm, _ = nuevo(carpeta, "permisos-efectivos.json")
    perm.crear("jefe", "clave-larga-1", "admin")
    perm.crear("tecnico", "clave-larga-1", "operador")
    perm.crear("cliente", "clave-larga-1", "lector")

    comprobar("el rol solo: admin lo puede todo",
              perm.permisos(perm.obtener("jefe")) == set(usr.PERMISOS))
    comprobar("el rol solo: operador tiene lo suyo",
              perm.permisos(perm.obtener("tecnico"))
              == set(usr.ROLES_INICIALES["operador"]))
    comprobar("el rol solo: lector solo ve",
              perm.permisos(perm.obtener("cliente")) == {"ver"})

    # Con 'mas': la excepcion de "este de aqui tambien importa el Excel".
    comprobar("permisos_mas suma sobre el rol",
              perm.actualizar("cliente", permisos_mas=["equipos.importar"]) == []
              and perm.permisos(perm.obtener("cliente"))
              == {"ver", "equipos.importar"})
    comprobar("y puede() lo ve",
              perm.puede(perm.obtener("cliente"), "equipos.importar"))

    # Con 'menos': se le quita algo que el rol si daba.
    comprobar("permisos_menos resta sobre el rol",
              perm.actualizar("tecnico", permisos_menos=["equipos.baja"]) == []
              and "equipos.baja" not in perm.permisos(perm.obtener("tecnico")))
    comprobar("pero el resto del rol sigue ahi",
              "equipos.crear" in perm.permisos(perm.obtener("tecnico")))
    comprobar("y puede() dice que no",
              not perm.puede(perm.obtener("tecnico"), "equipos.baja"))

    # El conflicto: en 'mas' y en 'menos' a la vez. Gana 'menos', porque quitar
    # es la operacion segura.
    comprobar("un permiso en mas y en menos a la vez NO se concede",
              perm.actualizar("cliente",
                              permisos_mas=["ajustes", "diferencias"],
                              permisos_menos=["ajustes"]) == []
              and perm.permisos(perm.obtener("cliente")) == {"ver", "diferencias"})
    comprobar("quitar gana tambien sobre lo que daba el rol y lo repetia mas",
              perm.actualizar("tecnico",
                              permisos_mas=["equipos.crear"],
                              permisos_menos=["equipos.crear"]) == []
              and "equipos.crear" not in perm.permisos(perm.obtener("tecnico")))

    # Permisos desconocidos guardados en el archivo (uno retirado, por ejemplo):
    # se ignoran, pero la cuenta sigue cargando y entrando.
    ruta_perm = perm.ruta
    datos = json.loads(ruta_perm.read_text(encoding="utf-8"))
    for fila in datos["usuarios"]:
        if fila["nombre"] == "cliente":
            fila["permisos_mas"] = ["diferencias", "telepatia"]
            fila["permisos_menos"] = ["adivinar"]
    datos["roles"]["lector"] = ["ver", "permiso-retirado"]
    ruta_perm.write_text(json.dumps(datos), encoding="utf-8")
    os.utime(ruta_perm, (time.time() + 1, time.time() + 1))
    comprobar("un permiso desconocido en el archivo no revienta ni cuenta",
              perm.permisos(perm.obtener("cliente")) == {"ver", "diferencias"})
    comprobar("y la cuenta sigue entrando con normalidad",
              perm.autenticar("cliente", "clave-larga-1") is not None)
    comprobar("un permiso retirado en el rol tampoco cuenta",
              "permiso-retirado" not in perm.permisos(perm.obtener("cliente")))

    comprobar("un rol que no existe no da NADA",
              perm.crear("raro", "clave-larga-1", "lector") == []
              and perm.actualizar("raro", rol="lector") == []
              and _con_rol(perm, "raro", "administrador")
              and perm.permisos(perm.obtener("raro")) == set())
    comprobar("un permiso desconocido siempre es False en puede()",
              not perm.puede(perm.obtener("jefe"), "borrar-todo")
              and not perm.puede(perm.obtener("jefe"), ""))
    comprobar("cada permiso del contrato tiene su etiqueta para la web",
              all(p in usr.ETIQUETAS_PERMISO and usr.ETIQUETAS_PERMISO[p]
                  for p in usr.PERMISOS))

    # 13. Alcance: sobre que equipos actua cada cuenta.
    todo = usr.Alcance(todo=True)
    comprobar("alcance todo: edita cualquier cosa",
              todo.nivel("Acme", "core-01") == usr.EDITAR
              and todo.puede_editar("Empresa Que No Existe", "loquesea"))

    vacio = usr.Alcance()
    comprobar("alcance vacio: no ve NADA",
              vacio.nivel("Acme", "core-01") is None
              and not vacio.puede_ver("Acme", "core-01"))

    solo_empresa = usr.Alcance(empresas={"Acme S.A.": usr.VER})
    comprobar("empresa sola: ve sus equipos",
              solo_empresa.puede_ver("Acme S.A.", "core-01"))
    comprobar("pero no los edita",
              not solo_empresa.puede_editar("Acme S.A.", "core-01"))
    comprobar("y no ve los de otra empresa",
              not solo_empresa.puede_ver("Bravo Telecom", "rb-01"))

    # La comparacion de empresa va por el slug de inventory.sanear_empresa: el
    # nombre lo escribe una persona en el Excel y cambia de mayusculas y de
    # espacios cada dia, pero es el mismo cliente y la misma carpeta del repo.
    comprobar("la empresa no distingue mayusculas",
              solo_empresa.puede_ver("ACME S.A.", "core-01"))
    comprobar("ni los espacios de sobra",
              solo_empresa.puede_ver("   Acme   S.A.  ", "core-01"))
    comprobar("ni las tildes (mismo cliente, misma carpeta)",
              usr.Alcance(empresas={"Telefonica": usr.VER})
              .puede_ver("Telefónica", "core-01"))
    comprobar("pero una empresa de verdad distinta sigue fuera",
              not solo_empresa.puede_ver("Acme Sur", "core-01"))

    solo_equipo = usr.Alcance(equipos={"bravo-rb1": usr.VER})
    comprobar("equipo solo: se ve ese equipo aunque su empresa no se vea",
              solo_equipo.puede_ver("Bravo Telecom", "bravo-rb1"))
    comprobar("y no el resto de esa empresa",
              not solo_equipo.puede_ver("Bravo Telecom", "bravo-rb2"))
    comprobar("el nombre del equipo tampoco distingue mayusculas",
              solo_equipo.puede_ver("Bravo Telecom", "BRAVO-RB1"))

    mezcla = usr.Alcance(empresas={"Acme": usr.VER},
                         equipos={"acme-core": usr.EDITAR})
    comprobar("un equipo MEJORA lo que da su empresa (ver -> editar)",
              mezcla.puede_editar("Acme", "acme-core"))
    comprobar("y el resto de la empresa se queda en ver",
              mezcla.nivel("Acme", "acme-bts") == usr.VER)

    resta = usr.Alcance(empresas={"Acme": usr.EDITAR},
                        equipos={"acme-core": usr.VER})
    comprobar("un equipo NUNCA resta: sigue pudiendo editarse",
              resta.puede_editar("Acme", "acme-core"))

    comprobar("un nivel que no se entiende no concede nada",
              usr.Alcance(empresas={"Acme": "lectura"}).nivel("Acme", "x") is None)

    # Ida y vuelta por JSON: es como viaja al archivo y como vuelve.
    for original in (todo, vacio, solo_empresa, solo_equipo, mezcla):
        copia = usr.Alcance.desde_dict(original.como_dict())
        comprobar(f"ida y vuelta como_dict/desde_dict: {original.como_dict()}",
                  copia == original)
        comprobar("y el diccionario es JSON de verdad",
                  json.loads(json.dumps(original.como_dict()))
                  == original.como_dict())
    comprobar("desde_dict con basura devuelve un alcance vacio",
              usr.Alcance.desde_dict(None) == usr.Alcance()
              and usr.Alcance.desde_dict("hola") == usr.Alcance()
              and usr.Alcance.desde_dict({"empresas": []}) == usr.Alcance())

    # El alcance viaja entero al archivo y vuelve igual.
    alc, ruta_alc = nuevo(carpeta, "alcance.json")
    alc.crear("jefe", "clave-larga-1", "admin")
    alc.crear("acme", "clave-larga-1", "lector", alcance=mezcla)
    otro = usr.Usuarios(ruta_alc, iteraciones=ITER)
    comprobar("el alcance sobrevive al archivo y al proceso",
              otro.obtener("acme").alcance == mezcla)

    # 14. Roles editables: crear, editar y borrar.
    rol, _ = nuevo(carpeta, "roles.json")
    rol.crear("jefe", "clave-larga-1", "admin")
    comprobar("de fabrica estan los tres roles iniciales",
              sorted(rol.roles()) == ["admin", "lector", "operador"])

    comprobar("crear un rol nuevo no da errores",
              rol.guardar_rol("facturacion", ["ver", "diferencias"]) == [])
    comprobar("y aparece con sus permisos",
              rol.roles().get("facturacion") == ["ver", "diferencias"])
    comprobar("editar un rol lo reemplaza entero",
              rol.guardar_rol("facturacion", ["ver"]) == []
              and rol.roles().get("facturacion") == ["ver"])
    comprobar("el nombre del rol se guarda en minusculas",
              rol.guardar_rol("Soporte", ["ver"]) == []
              and "soporte" in rol.roles() and "Soporte" not in rol.roles())

    comprobar("un rol con un permiso inventado se rechaza",
              rol.guardar_rol("chapuza", ["ver", "volar"]) != []
              and "chapuza" not in rol.roles())
    for malo in ["", " ", "x", "y" * 33, "con espacios", "acentuadó", "rol@casa"]:
        comprobar(f"nombre de rol invalido: {malo!r}",
                  rol.guardar_rol(malo, ["ver"]) != [])

    comprobar("el rol nuevo se puede asignar",
              rol.crear("contable", "clave-larga-1", "facturacion") == []
              and rol.permisos(rol.obtener("contable")) == {"ver"})

    comprobar("un rol en uso no se puede borrar",
              rol.borrar_rol("facturacion") != [])
    comprobar("y el mensaje dice cuantos usuarios lo tienen",
              "1" in " ".join(rol.borrar_rol("facturacion")))
    comprobar("el rol sigue ahi tras el intento",
              "facturacion" in rol.roles())
    comprobar("liberandolo si se puede borrar",
              rol.actualizar("contable", rol="lector") == []
              and rol.borrar_rol("facturacion") == []
              and "facturacion" not in rol.roles())
    comprobar("borrar un rol que no existe avisa",
              rol.borrar_rol("facturacion") != [])
    comprobar("el rol 'admin' NUNCA se borra, ni estando libre",
              rol.actualizar("jefe", rol="admin") == []
              and rol.borrar_rol("admin") != []
              and "admin" in rol.roles())
    comprobar("los roles sobreviven al proceso",
              usr.Usuarios(rol.ruta, iteraciones=ITER).roles() == rol.roles())

    # 15. LA INVARIANTE: nunca sin un usuario con 'usuarios' + alcance a todo.
    #     Es lo que sustituye al viejo "no te quedes sin admin": con roles
    #     editables, el nombre del rol ya no garantiza nada.
    solo, _ = nuevo(carpeta, "solo-admin.json")
    solo.crear("jefe", "clave-larga-1", "admin")            # el unico total
    solo.crear("tecnico", "clave-larga-1", "operador")
    solo.crear("cliente", "clave-larga-1", "lector")
    # Un admin que administra pero NO lo ve todo: no sostiene la invariante.
    solo.crear("admin.acme", "clave-larga-1", "admin",
               alcance=usr.Alcance(empresas={"Acme": usr.EDITAR}))

    comprobar("el unico administrador total es el que tiene las dos cosas",
              solo.puede(solo.obtener("jefe"), "usuarios")
              and solo.obtener("jefe").alcance.todo
              and solo.puede(solo.obtener("admin.acme"), "usuarios")
              and not solo.obtener("admin.acme").alcance.todo)

    def explica(errores):
        texto = " ".join(errores).lower()
        return bool(errores) and "usuarios" in texto and "empresas" in texto

    # Camino 1: borrarlo.
    errores = solo.borrar("jefe")
    comprobar("no se puede borrar al ultimo administrador total", errores != [])
    comprobar("y el mensaje explica que pasaria y como evitarlo", explica(errores))
    comprobar("sigue ahi despues del intento", solo.obtener("jefe") is not None)

    # Camino 2: cambiarle el rol.
    errores = solo.cambiar_rol("jefe", "lector")
    comprobar("tampoco se le puede cambiar el rol a uno sin 'usuarios'",
              errores != [])
    comprobar("y ese mensaje tambien lo explica", explica(errores))
    comprobar("el rol no cambio", solo.obtener("jefe").rol == "admin")
    comprobar("degradarlo a operador tampoco cuela",
              solo.cambiar_rol("jefe", "operador") != [])

    # Camino 3: quitarle el permiso con permisos_menos (el rol sigue siendo
    # admin, pero la excepcion se lo quita: mismo resultado, mas silencioso).
    errores = solo.actualizar("jefe", permisos_menos=["usuarios"])
    comprobar("no se le puede quitar 'usuarios' con permisos_menos", errores != [])
    comprobar("y se explica", explica(errores))
    comprobar("el permiso sigue ahi", solo.puede(solo.obtener("jefe"), "usuarios"))
    comprobar("quitarle OTRO permiso si se puede",
              solo.actualizar("jefe", permisos_menos=["equipos.importar"]) == []
              and not solo.puede(solo.obtener("jefe"), "equipos.importar"))

    # Camino 4: recortarle el alcance.
    errores = solo.actualizar("jefe",
                              alcance=usr.Alcance(empresas={"Acme": usr.EDITAR}))
    comprobar("no se le puede recortar el alcance", errores != [])
    comprobar("y se explica", explica(errores))
    comprobar("el alcance sigue siendo todo", solo.obtener("jefe").alcance.todo)
    comprobar("dejarlo en un alcance vacio tampoco cuela",
              solo.actualizar("jefe", alcance=usr.Alcance()) != [])
    comprobar("ampliarle el alcance (que ya es todo) no molesta",
              solo.actualizar("jefe", alcance=usr.Alcance(todo=True)) == [])

    # Camino 5 (indirecto): quitarle 'usuarios' AL ROL que se lo daba.
    sin_usuarios = [p for p in usr.PERMISOS if p != "usuarios"]
    errores = solo.guardar_rol("admin", sin_usuarios)
    comprobar("no se le puede quitar 'usuarios' al rol que sostiene al ultimo",
              errores != [])
    comprobar("y se explica", explica(errores))
    comprobar("el rol admin sigue dando 'usuarios'",
              "usuarios" in solo.roles()["admin"])
    comprobar("editar el rol sin tocar 'usuarios' si se puede",
              solo.guardar_rol("admin", [p for p in usr.PERMISOS
                                         if p != "equipos.importar"]) == []
              and solo.guardar_rol("admin", list(usr.PERMISOS)) == [])

    # Camino 6 (indirecto): borrar el rol que lo sostiene.
    errores = solo.borrar_rol("admin")
    comprobar("no se puede borrar el rol que sostiene al ultimo total",
              errores != [])
    comprobar("admin sigue existiendo", "admin" in solo.roles())

    # Y a los demas si se les puede hacer de todo: la regla es "que quede uno".
    comprobar("a un admin con alcance limitado si se le puede quitar el rol",
              solo.cambiar_rol("admin.acme", "lector") == [])
    comprobar("y borrar a quien no es administrador total tambien",
              solo.borrar("cliente") == [])

    # Con un segundo administrador total, el primero deja de estar blindado.
    comprobar("se crea un segundo administrador total",
              solo.crear("jefa", "clave-larga-1", "admin",
                         alcance=usr.Alcance(todo=True)) == [])
    comprobar("ahora si se puede degradar al primero",
              solo.cambiar_rol("jefe", "operador") == [])
    comprobar("pero a la que queda sola no se la puede tocar",
              solo.cambiar_rol("jefa", "lector") != []
              and solo.borrar("jefa") != []
              and solo.actualizar("jefa", permisos_menos=["usuarios"]) != []
              and solo.actualizar("jefa", alcance=usr.Alcance()) != [])
    comprobar("siempre queda al menos un administrador total",
              any(solo.puede(x, "usuarios") and x.alcance.todo
                  for x in solo.listar()))

    # El camino indirecto con un rol PROPIO, que es como pasara de verdad:
    # alguien se hace su rol 'jefatura' y luego lo edita sin pensar.
    comprobar("se puede montar la invariante sobre un rol propio",
              solo.guardar_rol("jefatura", ["ver", "usuarios"]) == []
              and solo.actualizar("jefa", rol="jefatura") == []
              and solo.puede(solo.obtener("jefa"), "usuarios"))
    comprobar("y entonces ese rol propio queda igual de protegido",
              solo.guardar_rol("jefatura", ["ver"]) != []
              and solo.borrar_rol("jefatura") != [])
    comprobar("el 'admin' de reserva sigue sin poder borrarse",
              solo.borrar_rol("admin") != [])

    # 16. Sembrado desde config.yaml: la migracion de quien ya tenia el panel.
    semilla, ruta_semilla = nuevo(carpeta, "semilla.json")
    hash_yaml = ses.hashear("la-del-yaml-2026", iteraciones=ITER)
    comprobar("sembrar crea el archivo y lo dice",
              semilla.sembrar("operaciones", hash_yaml) is True)
    comprobar("el archivo existe", ruta_semilla.exists())
    sembrado = semilla.obtener("operaciones")
    comprobar("la cuenta migrada es administradora",
              sembrado is not None and sembrado.rol == "admin")
    comprobar("y con alcance a todo: era la unica cuenta y lo veia todo",
              sembrado.alcance.todo)
    comprobar("asi que sostiene la invariante desde el primer arranque",
              semilla.puede(sembrado, "usuarios") and sembrado.alcance.todo)
    comprobar("el hash del YAML se copio tal cual, sin volver a hashearlo",
              sembrado.clave_hash == hash_yaml)
    comprobar("y por eso la clave de siempre sigue entrando",
              semilla.autenticar("operaciones", "la-del-yaml-2026") is not None)

    comprobar("sembrar otra vez no hace nada",
              semilla.sembrar("operaciones", hash_yaml) is False)
    comprobar("ni siquiera con otro usuario: no pisa lo que hay",
              semilla.sembrar("otro", ses.hashear("x" * 10, iteraciones=ITER)) is False)
    comprobar("sigue habiendo una sola cuenta", len(semilla.listar()) == 1)
    comprobar("y sigue siendo la de siempre",
              semilla.obtener("otro") is None
              and semilla.obtener("operaciones") is not None)

    # Y desde otro proceso (el panel arranca de nuevo) tampoco resiembra.
    reinicio = usr.Usuarios(ruta_semilla, iteraciones=ITER)
    comprobar("un reinicio del panel no resiembra",
              reinicio.sembrar("operaciones", hash_yaml) is False)

    # Sembrar sin nada que migrar no crea un archivo vacio ni revienta.
    vacia, ruta_vacia = nuevo(carpeta, "sin-semilla.json")
    comprobar("sembrar sin usuario no hace nada", vacia.sembrar("", hash_yaml) is False)
    comprobar("sembrar sin hash tampoco", vacia.sembrar("alguien", "") is False)
    comprobar("y no deja un archivo a medias", not ruta_vacia.exists())

    # Sembrar despues de crear cuentas a mano tampoco pisa.
    poblada, _ = nuevo(carpeta, "poblada.json")
    poblada.crear("jefe", "clave-larga-1", "admin")
    comprobar("sembrar sobre un archivo con cuentas no hace nada",
              poblada.sembrar("operaciones", hash_yaml) is False
              and len(poblada.listar()) == 1)

    hash_bueno = ses.hashear("clave-larga-1", iteraciones=ITER)

    # 17. Migracion de formato 1 a formato 2, al leer y sin perder nada.
    ruta_v1 = carpeta / "formato1.json"
    ruta_v1.write_text(
        json.dumps({
            "formato": 1,
            "usuarios": [
                {"nombre": "jefe", "rol": "admin", "clave_hash": hash_bueno,
                 "creado": "2024-03-01T10:00:00+00:00",
                 "ultimo_acceso": "2026-07-01T08:00:00+00:00"},
                {"nombre": "tecnico", "rol": "operador", "clave_hash": hash_bueno},
                {"nombre": "cliente", "rol": "lector", "clave_hash": hash_bueno},
            ],
        }),
        encoding="utf-8",
    )
    viejo = usr.Usuarios(ruta_v1, iteraciones=ITER)
    migrados = {x.nombre: x for x in viejo.listar()}
    comprobar("la migracion no pierde ninguna cuenta",
              sorted(migrados) == ["cliente", "jefe", "tecnico"])
    comprobar("cada uno conserva su rol",
              migrados["jefe"].rol == "admin"
              and migrados["tecnico"].rol == "operador"
              and migrados["cliente"].rol == "lector")
    comprobar("todos reciben alcance a todo: es lo que ya podian ver",
              all(x.alcance.todo for x in migrados.values()))
    comprobar("y sin excepciones de permisos: nadie gana ni pierde nada",
              all(x.permisos_mas == [] and x.permisos_menos == []
                  for x in migrados.values()))
    comprobar("las fechas se conservan",
              migrados["jefe"].creado == "2024-03-01T10:00:00+00:00"
              and migrados["jefe"].ultimo_acceso == "2026-07-01T08:00:00+00:00")
    comprobar("las claves de siempre siguen entrando",
              viejo.autenticar("tecnico", "clave-larga-1") is not None)

    escrito = json.loads(ruta_v1.read_text(encoding="utf-8"))
    comprobar("el archivo queda escrito en el formato nuevo",
              escrito.get("formato") == usr.FORMATO)
    comprobar("y los tres roles de siempre pasan a ser datos",
              escrito.get("roles") == {k: list(v)
                                       for k, v in usr.ROLES_INICIALES.items()})
    comprobar("los permisos efectivos son los de antes de migrar",
              viejo.permisos(migrados["tecnico"])
              == set(usr.ROLES_INICIALES["operador"]))
    comprobar("y el admin migrado sostiene la invariante",
              viejo.puede(migrados["jefe"], "usuarios")
              and migrados["jefe"].alcance.todo)

    # Idempotente: volver a leerlo (otro proceso, o el mismo) no lo vuelve a
    # tocar. Se compara el texto entero, no solo el formato.
    texto_tras_migrar = ruta_v1.read_text(encoding="utf-8")
    marca_tras_migrar = ruta_v1.stat().st_mtime_ns
    otra_vez = usr.Usuarios(ruta_v1, iteraciones=ITER)
    otra_vez.listar()
    otra_vez.roles()
    otra_vez.listar()
    comprobar("releer un archivo ya migrado no lo reescribe",
              ruta_v1.read_text(encoding="utf-8") == texto_tras_migrar
              and ruta_v1.stat().st_mtime_ns == marca_tras_migrar)

    # Un archivo que YA es formato 2 no se toca, y sus roles propios se
    # respetan (no se pisan con ROLES_INICIALES).
    ruta_v2 = carpeta / "formato2.json"
    contenido_v2 = json.dumps({
        "formato": 2,
        "roles": {"admin": list(usr.PERMISOS), "facturacion": ["ver"]},
        "usuarios": [
            {"nombre": "jefe", "rol": "admin", "clave_hash": hash_bueno,
             "creado": "", "ultimo_acceso": "",
             "permisos_mas": [], "permisos_menos": [],
             "alcance": {"todo": True, "empresas": {}, "equipos": {}}},
            {"nombre": "contable", "rol": "facturacion", "clave_hash": hash_bueno,
             "creado": "", "ultimo_acceso": "",
             "permisos_mas": ["diferencias"], "permisos_menos": [],
             "alcance": {"todo": False, "empresas": {"Acme S.A.": "ver"},
                         "equipos": {}}},
        ],
    }, indent=2)
    ruta_v2.write_text(contenido_v2, encoding="utf-8")
    nuevo_formato = usr.Usuarios(ruta_v2, iteraciones=ITER)
    comprobar("un archivo ya en formato 2 se lee tal cual",
              sorted(x.nombre for x in nuevo_formato.listar())
              == ["contable", "jefe"])
    comprobar("y no se reescribe al leerlo",
              ruta_v2.read_text(encoding="utf-8") == contenido_v2)
    comprobar("sus roles propios se respetan",
              nuevo_formato.roles().get("facturacion") == ["ver"])
    comprobar("y las excepciones y el alcance se leen enteros",
              nuevo_formato.permisos(nuevo_formato.obtener("contable"))
              == {"ver", "diferencias"}
              and nuevo_formato.obtener("contable").alcance
              .puede_ver("acme s.a.", "core-01"))

    # 18. Archivo corrupto: nadie entra, nada se sobreescribe, todo se explica.
    roto, ruta_rota = nuevo(carpeta, "roto.json")
    roto.crear("jefe", "clave-larga-1", "admin")
    ruta_rota.write_text("{esto no es json, se corto a medias", encoding="utf-8")

    comprobar("con el archivo corrupto listar devuelve vacio", roto.listar() == [])
    comprobar("y obtener devuelve None", roto.obtener("jefe") is None)
    comprobar("y NO se autentica a nadie, ni con la clave buena",
              roto.autenticar("jefe", "clave-larga-1") is None)

    for descripcion, accion in [
        ("crear", lambda: roto.crear("nuevo", "clave-larga-1", "admin")),
        ("cambiar_clave", lambda: roto.cambiar_clave("jefe", "clave-larga-1")),
        ("cambiar_rol", lambda: roto.cambiar_rol("jefe", "lector")),
        ("actualizar", lambda: roto.actualizar("jefe", rol="lector")),
        ("borrar", lambda: roto.borrar("jefe")),
        ("guardar_rol", lambda: roto.guardar_rol("nuevo", ["ver"])),
        ("borrar_rol", lambda: roto.borrar_rol("lector")),
        ("sembrar", lambda: roto.sembrar("jefe", hash_yaml)),
    ]:
        try:
            accion()
        except usr.ErrorUsuarios as exc:
            comprobar(f"{descripcion} lanza ErrorUsuarios con el archivo corrupto",
                      bool(str(exc)) and str(ruta_rota) in str(exc))
        except Exception as exc:  # noqa: BLE001 - cualquier otra cosa es un fallo
            comprobar(f"{descripcion} lanza ErrorUsuarios y no {type(exc).__name__}",
                      False)
        else:
            comprobar(f"{descripcion} deberia haber lanzado ErrorUsuarios", False)

    comprobar("el archivo corrupto sigue tal cual, sin pisarlo",
              ruta_rota.read_text(encoding="utf-8").startswith("{esto no es json"))

    # Otras formas de estar roto: JSON valido pero sin la estructura esperada.
    for basura in ['[]', '"hola"', '{"formato": 2}', '{"usuarios": {}}', '', '   ']:
        ruta_rota.write_text(basura, encoding="utf-8")
        malo = usr.Usuarios(ruta_rota, iteraciones=ITER)
        comprobar(f"un archivo con {basura!r} no autentica a nadie",
                  malo.listar() == [] and malo.autenticar("jefe", "clave-larga-1") is None)

    # Una fila suelta mal escrita se salta, pero las demas siguen valiendo:
    # perder una cuenta es malo, dejar fuera a las otras es peor.
    ruta_rota.write_text(
        json.dumps({
            "formato": 2,
            "roles": {"admin": list(usr.PERMISOS)},
            "usuarios": [
                {"nombre": "bueno", "rol": "admin", "clave_hash": hash_bueno,
                 "alcance": {"todo": True}},
                {"nombre": "sinhash", "rol": "admin"},
                {"rol": "admin", "clave_hash": hash_bueno},
                "esto no es un usuario",
            ],
        }),
        encoding="utf-8",
    )
    parcial = usr.Usuarios(ruta_rota, iteraciones=ITER)
    comprobar("las filas basura se saltan y la buena se conserva",
              [x.nombre for x in parcial.listar()] == ["bueno"])
    comprobar("y esa cuenta entra con normalidad",
              parcial.autenticar("bueno", "clave-larga-1") is not None)

    # Un rol desconocido guardado en el archivo se conserva tal cual, pero no
    # sirve para nada: si el archivo se corrompe o alguien escribe
    # 'administrador' a mano, el fallo tiene que ser NEGAR.
    ruta_rota.write_text(
        json.dumps({
            "formato": 2,
            "roles": {"admin": list(usr.PERMISOS)},
            "usuarios": [
                {"nombre": "raro", "rol": "administrador",
                 "clave_hash": hash_bueno, "alcance": {"todo": True}},
            ],
        }),
        encoding="utf-8",
    )
    fallido = usr.Usuarios(ruta_rota, iteraciones=ITER)
    entrada = fallido.autenticar("raro", "clave-larga-1")
    comprobar("un rol desconocido en el archivo si deja entrar",
              entrada is not None)
    comprobar("pero no le permite absolutamente nada",
              fallido.permisos(entrada) == set()
              and not any(fallido.puede(entrada, p) for p in usr.PERMISOS))
    comprobar("y con el rol roto, la invariante no bloquea el arreglo",
              fallido.crear("rescate", "clave-larga-1", "admin",
                            alcance=usr.Alcance(todo=True)) == []
              and fallido.borrar("raro") == [])

    # 19. Relectura: el programador y el panel son procesos distintos, y un
    #     admin puede editar el archivo a mano. Cachear sin comprobar dejaria
    #     al panel viendo cuentas que ya no existen.
    a, ruta_ab = nuevo(carpeta, "compartido.json")
    b = usr.Usuarios(ruta_ab, iteraciones=ITER)
    a.crear("jefe", "clave-larga-1", "admin")
    comprobar("el segundo proceso ve la cuenta creada por el primero",
              b.obtener("jefe") is not None)

    # b ya tiene el archivo cacheado; a lo cambia por debajo.
    a.crear("tecnico", "clave-larga-1", "operador")
    comprobar("el alta hecha por el otro proceso se ve sin reiniciar",
              b.obtener("tecnico") is not None)

    a.cambiar_rol("tecnico", "lector")
    comprobar("el cambio de rol tambien se ve",
              b.obtener("tecnico").rol == "lector")

    a.actualizar("tecnico", alcance=usr.Alcance(empresas={"Acme": usr.EDITAR}))
    comprobar("y el cambio de alcance",
              b.obtener("tecnico").alcance.puede_editar("Acme", "core-01"))

    a.guardar_rol("facturacion", ["ver"])
    comprobar("un rol nuevo se ve desde el otro proceso",
              "facturacion" in b.roles())

    a.cambiar_clave("tecnico", "clave-nueva-16")
    comprobar("la clave nueva vale desde el otro proceso",
              b.autenticar("tecnico", "clave-nueva-16") is not None)
    comprobar("y la vieja ya no",
              b.autenticar("tecnico", "clave-larga-1") is None)

    a.borrar("tecnico")
    comprobar("la baja hecha por el otro proceso se ve",
              b.obtener("tecnico") is None)
    comprobar("y el borrado ya no puede entrar",
              b.autenticar("tecnico", "clave-nueva-16") is None)

    # Edicion a mano del archivo, que es el otro caso real.
    datos = json.loads(ruta_ab.read_text(encoding="utf-8"))
    datos["usuarios"].append(
        {"nombre": "amano", "rol": "lector", "clave_hash": hash_bueno}
    )
    # Se toca la fecha del archivo por si el sistema de archivos tiene poca
    # resolucion y la escritura cae dentro del mismo tic que la anterior.
    ruta_ab.write_text(json.dumps(datos), encoding="utf-8")
    os.utime(ruta_ab, (time.time() + 1, time.time() + 1))
    comprobar("una cuenta anadida a mano al archivo se ve",
              b.obtener("amano") is not None)
    comprobar("y entra con su clave",
              b.autenticar("amano", "clave-larga-1") is not None)

    # Que el archivo desaparezca tampoco deja el cache mintiendo.
    ruta_ab.unlink()
    comprobar("si el archivo desaparece, las cuentas desaparecen con el",
              b.listar() == [] and b.obtener("jefe") is None)

    # 20. Permisos del archivo: lleva los hashes de todo el mundo.
    fichero, ruta_fichero = nuevo(carpeta, "permisos-archivo.json")
    fichero.crear("jefe", "clave-larga-1", "admin")
    if os.name == "nt":
        # En Windows chmod solo toca el bit de solo lectura, asi que aqui la
        # comprobacion no dice nada; lo que importa es el Debian del servicio.
        comprobar("en Windows los permisos no se comprueban (chmod es simbolico)",
                  ruta_fichero.exists())
    else:
        comprobar("el archivo de usuarios queda en 0600",
                  (ruta_fichero.stat().st_mode & 0o777) == 0o600)

    # 21. El tiempo de respuesta no puede delatar que cuentas existen.
    #     Con las iteraciones de prueba (1000) todo tarda microsegundos y la
    #     medida seria puro ruido, asi que este caso usa un coste mas alto.
    #     El margen es generoso a proposito: se busca cazar el "return None
    #     inmediato", no medir con precision.
    reloj, _ = nuevo(carpeta, "tiempos.json", iteraciones=60_000)
    reloj.crear("jefe", "clave-larga-1", "admin")

    inicio = time.perf_counter()
    reloj.autenticar("jefe", "clave-equivocada")
    existente = time.perf_counter() - inicio

    inicio = time.perf_counter()
    reloj.autenticar("no-existe-nadie-asi", "clave-equivocada")
    inexistente = time.perf_counter() - inicio

    comprobar(
        "un usuario inexistente tarda lo mismo que uno con la clave mal "
        f"({inexistente * 1000:.0f}ms vs {existente * 1000:.0f}ms)",
        inexistente >= existente * 0.5,
    )


def _con_rol(almacen, nombre: str, rol: str) -> bool:
    """Le pone a mano un rol que no existe, como haria un archivo corrupto.

    Por la API no se puede (y esa es justo la gracia): hay que editar el
    archivo. Devuelve True si el cambio quedo puesto.
    """
    datos = json.loads(almacen.ruta.read_text(encoding="utf-8"))
    for fila in datos["usuarios"]:
        if fila["nombre"] == nombre:
            fila["rol"] = rol
    almacen.ruta.write_text(json.dumps(datos), encoding="utf-8")
    os.utime(almacen.ruta, (time.time() + 2, time.time() + 2))
    cuenta = almacen.obtener(nombre)
    return cuenta is not None and cuenta.rol == rol


if __name__ == "__main__":
    main()
