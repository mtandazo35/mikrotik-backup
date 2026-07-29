"""Prueba de la autenticacion del panel: hash de la clave y sesiones.

Lo que se comprueba aqui es lo que separa el panel de estar abierto de par
en par:

  - la clave del YAML no se puede leer ni comparar a ojo, y dos equipos con
    la misma clave no lo parecen
  - un YAML mal escrito devuelve False en vez de tumbar el panel
  - un token cerrado o caducado deja de valer de verdad
  - cada token dice de QUIEN es, y echar a una cuenta le cierra todas sus
    sesiones sin tocar las de los demas
  - probar claves a lo bruto sale caro, y el castigo es por IP
  - nada de esto se rompe con los hilos de ThreadingHTTPServer encima

No levanta el servidor ni toca la red.

Ejecutar:  python -m tests.test_sesion
"""

import base64
import threading

from mkbackup import sesion as ses

FALLOS = []

# Coste de pbkdf2 en las pruebas. Las 240.000 iteraciones de produccion son
# medio segundo por hash; con las decenas de hashes que se calculan aqui la
# prueba tardaria mas que todo el resto del proyecto junto. Lo que se prueba
# es el formato y la comparacion, no lo caro que sale, asi que basta con 1000.
ITER = 1000


def comprobar(descripcion: str, condicion: bool) -> None:
    print(f"[{'OK  ' if condicion else 'FALLA'}] {descripcion}")
    if not condicion:
        FALLOS.append(descripcion)


def b64(texto: bytes) -> str:
    return base64.b64encode(texto).decode("ascii")


class SesionesReloj(ses.Sesiones):
    """Sesiones con el reloj en la mano para no esperar 8 horas de verdad.

    Se sobreescribe `_ahora` en una subclase en vez de parchear el modulo:
    dentro de Sesiones siempre se llama como `self._ahora()`, asi que la
    subclase lo intercepta entero sin tocar nada global ni dejar residuos
    para las demas pruebas.
    """

    def __init__(self, *args, **kwargs):
        self.reloj = 1000.0
        super().__init__(*args, **kwargs)

    def _ahora(self) -> float:
        return self.reloj

    def avanzar(self, segundos: float) -> None:
        self.reloj += segundos


def main() -> None:
    # 1. Hash bien formado y clave correcta.
    clave = "Contrasena-Del-Panel-2026"
    guardado = ses.hashear(clave, iteraciones=ITER)
    comprobar("el hash tiene el formato que espera la configuracion",
              ses.formato_valido(guardado))
    comprobar("el hash lleva el algoritmo por delante",
              guardado.startswith(f"{ses.ALGORITMO}$"))
    comprobar("el hash guarda las iteraciones con las que se calculo",
              guardado.split("$")[1] == str(ITER))
    comprobar("la clave correcta verifica", ses.verificar(clave, guardado))
    comprobar("la clave en claro no aparece en el hash", clave not in guardado)

    # 2. La sal es aleatoria: dos equipos con la misma clave no se parecen.
    #    Sin esto, comparar dos configuraciones delataria que comparten clave.
    otro = ses.hashear(clave, iteraciones=ITER)
    comprobar("dos hashes de la misma clave salen distintos", guardado != otro)
    comprobar("y aun asi los dos verifican la misma clave",
              ses.verificar(clave, otro) and ses.verificar(clave, guardado))
    comprobar("con la misma sal el hash es reproducible",
              ses.hashear(clave, iteraciones=ITER, sal=b"0123456789abcdef")
              == ses.hashear(clave, iteraciones=ITER, sal=b"0123456789abcdef"))

    # 3. Todo lo que no sea la clave exacta se rechaza.
    comprobar("una clave equivocada no verifica",
              not ses.verificar("otra cosa", guardado))
    comprobar("la cadena vacia no verifica", not ses.verificar("", guardado))
    comprobar("cambiar mayusculas no cuela",
              not ses.verificar(clave.upper(), guardado))
    comprobar("cambiar minusculas tampoco",
              not ses.verificar(clave.lower(), guardado))
    comprobar("un espacio de mas no cuela",
              not ses.verificar(clave + " ", guardado))
    comprobar("un prefijo de la clave no cuela",
              not ses.verificar(clave[:-1], guardado))
    comprobar("el hash vacio no deja pasar a nadie",
              not ses.verificar("", ""))

    # 4. Un YAML mal escrito devuelve False; el panel no puede reventar por
    #    una linea que alguien copio a medias.
    basura = [
        "",
        "   ",
        "pbkdf2_sha256",
        "pbkdf2_sha256$1000",
        f"pbkdf2_sha256$1000${b64(b'sal')}",
        f"pbkdf2_sha256$1000${b64(b'sal')}${b64(b'hash')}$sobra",
        f"bcrypt$1000${b64(b'sal')}${b64(b'hash')}",
        f"md5$1000${b64(b'sal')}${b64(b'hash')}",
        f"$1000${b64(b'sal')}${b64(b'hash')}",
        f"pbkdf2_sha256$muchas${b64(b'sal')}${b64(b'hash')}",
        f"pbkdf2_sha256$0${b64(b'sal')}${b64(b'hash')}",
        f"pbkdf2_sha256$-5${b64(b'sal')}${b64(b'hash')}",
        f"pbkdf2_sha256$1000$no-es-base64!${b64(b'hash')}",
        f"pbkdf2_sha256$1000${b64(b'sal')}$tampoco*base64",
        f"pbkdf2_sha256$1000${b64(b'sal')}$AAAAA",
        "clave_en_claro_del_yaml_viejo",
    ]
    malos = [x for x in basura if ses.formato_valido(x)]
    comprobar(f"formato_valido rechaza los {len(basura)} valores basura"
              + (f" (cuela: {malos})" if malos else ""),
              not malos)

    lanzan = []
    acepta = []
    for x in basura:
        try:
            if ses.verificar(clave, x):
                acepta.append(x)
        except Exception as e:  # noqa: BLE001 - aqui interesa que NO pase
            lanzan.append(f"{x!r}: {e}")
    comprobar(f"verificar no lanza con un hash roto{' ' + str(lanzan) if lanzan else ''}",
              not lanzan)
    comprobar("y ningun hash roto deja entrar", not acepta)

    # Un hash del formato correcto pero con el contenido cambiado tampoco.
    partes = guardado.split("$")
    manipulado = "$".join(partes[:3] + [b64(b"hash que me acabo de inventar")])
    comprobar("un hash manipulado mantiene el formato pero no verifica",
              ses.formato_valido(manipulado) and not ses.verificar(clave, manipulado))

    # 5. Sesiones: abrir, validar, cerrar.
    s = ses.Sesiones()
    token = s.abrir("ana")
    comprobar("abrir devuelve un token", bool(token))
    comprobar("el token recien abierto vale", s.valida(token))
    comprobar("un token inventado no vale", not s.valida("token-de-mentira"))
    comprobar("None no vale", not s.valida(None))
    comprobar("la cadena vacia no vale", not s.valida(""))

    # valida ya no dice si vale, dice DE QUIEN es: cada peticion del panel
    # necesita el nombre para mirar el rol de esa cuenta.
    comprobar("valida devuelve el nombre del usuario que abrio la sesion",
              s.valida(token) == "ana")
    comprobar("un token que no vale devuelve None y no False",
              s.valida("token-de-mentira") is None and s.valida(None) is None)

    s.cerrar(token)
    comprobar("al cerrar sesion el token deja de valer", not s.valida(token))
    s.cerrar(token)  # cerrar dos veces (dos pestanas dando a Salir) no lanza
    s.cerrar(None)
    comprobar("cerrar dos veces o cerrar None no lanza", True)

    # 6. Dos sesiones distintas no comparten token.
    a, b = s.abrir("ana"), s.abrir("ana")
    comprobar("dos sesiones tienen tokens distintos", a != b)
    comprobar("las dos valen a la vez", s.valida(a) and s.valida(b))
    s.cerrar(a)
    comprobar("cerrar una sesion no toca la otra",
              not s.valida(a) and s.valida(b))
    comprobar("el token es largo de verdad", len(b) >= 40)

    # 6b. Usuarios distintos: las sesiones no se mezclan. Si un token de un
    #     lector se leyera como del admin, el panel entero seria de mentira.
    m = ses.Sesiones()
    t_ana = m.abrir("ana")
    t_luis = m.abrir("luis")
    comprobar("dos usuarios reciben tokens distintos", t_ana != t_luis)
    comprobar("cada token devuelve su propio usuario",
              m.valida(t_ana) == "ana" and m.valida(t_luis) == "luis")
    m.cerrar(t_ana)
    comprobar("cerrar la sesion de uno no toca la del otro",
              m.valida(t_ana) is None and m.valida(t_luis) == "luis")

    # 6c. cerrar_usuario: se cambia una clave, se baja un rol o se borra una
    #     cuenta y sus sesiones abiertas tienen que morir en ese momento.
    #     Mientras tanto, quien no tiene nada que ver sigue trabajando.
    e = ses.Sesiones()
    ana = [e.abrir("ana") for _ in range(3)]   # tres pestanas de la misma cuenta
    luis = [e.abrir("luis") for _ in range(2)]
    comprobar("las cinco sesiones valen antes de echar a nadie",
              all(e.valida(t) for t in ana + luis))
    cerradas = e.cerrar_usuario("ana")
    comprobar("cerrar_usuario devuelve cuantas sesiones cerro", cerradas == 3)
    comprobar("no queda ni una sesion de la cuenta cerrada",
              all(e.valida(t) is None for t in ana))
    comprobar("las sesiones de las demas cuentas siguen intactas",
              all(e.valida(t) == "luis" for t in luis))
    comprobar("y en memoria solo quedan las que siguen vivas",
              len(e._abiertas) == len(luis))
    comprobar("cerrar_usuario de una cuenta sin sesiones devuelve 0",
              e.cerrar_usuario("ana") == 0 and e.cerrar_usuario("nadie") == 0)
    comprobar("cerrar_usuario con nombre vacio no lanza ni cierra nada",
              e.cerrar_usuario("") == 0 and len(e._abiertas) == len(luis))

    # Y el caso que motiva todo esto: la cuenta se borra del archivo de
    # usuarios y su token, que seguiria valiendo horas, deja de valer ya.
    borrado = e.abrir("temporal")
    comprobar("el token del usuario borrado valia antes de borrarlo",
              e.valida(borrado) == "temporal")
    e.cerrar_usuario("temporal")
    comprobar("el token de un usuario borrado deja de valer",
              e.valida(borrado) is None)
    comprobar("y el token del borrado no revive al reabrir otra sesion suya",
              e.abrir("temporal") != borrado and e.valida(borrado) is None)

    # 7. Caducidad, con el reloj de mentira. Son DOS topes y se prueban por
    # separado, porque protegen de cosas distintas: el de inactividad, del
    # puesto que alguien deja abierto y se va; el absoluto, de una sesion
    # robada que se mantiene caliente a base de usarla.

    # 7a. Por inactividad: media hora sin tocar nada y fuera. Y usarla reinicia
    # la cuenta atras, que es lo que hace que trabajar no te eche.
    q = SesionesReloj(duracion_horas=8, inactividad_minutos=30)
    quieto = q.abrir("ana")
    q.avanzar(29 * 60)
    comprobar("a los 29 minutos quieta la sesion sigue viva",
              q.valida(quieto) == "ana")
    q.avanzar(29 * 60)
    comprobar("y usarla reinicia la cuenta atras", q.valida(quieto) == "ana")
    q.avanzar(30 * 60 + 1)
    comprobar("media hora sin tocar nada la cierra", not q.valida(quieto))
    # El token caducado no puede quedarse ocupando memoria para siempre.
    comprobar("el token caducado se tira del diccionario",
              quieto not in q._abiertas)

    # 7b. El refresco automatico NO cuenta como actividad. Esta es la prueba
    # que sostiene todo lo anterior: la pantalla de Estado pregunta sola cada
    # pocos segundos, asi que si esas peticiones reiniciaran el reloj bastaria
    # con dejar una pestana abierta para que la sesion no caducara nunca y el
    # tope de media hora seria decorativo.
    p = SesionesReloj(duracion_horas=8, inactividad_minutos=30)
    pestana = p.abrir("ana")
    for _ in range(20):
        p.avanzar(2 * 60)
        p.valida(pestana, refrescar=False)
    comprobar("una pestana que solo se refresca sola no mantiene viva la sesion",
              p.valida(pestana) is None)

    # 7c. Tope absoluto: aunque se use sin parar, a las 8 horas toca volver a
    # escribir la clave.
    r = SesionesReloj(duracion_horas=8, inactividad_minutos=30)
    viejo = r.abrir("ana")
    for _ in range(28):
        r.avanzar(15 * 60)
        r.valida(viejo)
    comprobar("usandola cada rato aguanta las 7 horas", r.valida(viejo) == "ana")
    for _ in range(5):
        r.avanzar(15 * 60)
        r.valida(viejo)
    comprobar("pero pasadas las 8 horas caduca igual aunque se este usando",
              not r.valida(viejo))
    nuevo = r.abrir("ana")
    comprobar("y se puede abrir otra despues de caducar la anterior",
              r.valida(nuevo) == "ana" and nuevo != viejo)

    # 8. Freno a la fuerza bruta.
    f = SesionesReloj(intentos_max=5, bloqueo_segundos=300)
    ip = "10.0.0.7"
    comprobar("una IP nueva no esta bloqueada", f.bloqueado(ip) == 0)
    for _ in range(4):
        f.fallo(ip)
    comprobar("con 4 fallos todavia se puede intentar", f.bloqueado(ip) == 0)
    f.fallo(ip)
    comprobar("al quinto fallo la IP queda bloqueada", f.bloqueado(ip) > 0)
    comprobar("el bloqueo dice cuantos segundos quedan",
              0 < f.bloqueado(ip) <= 301)

    # El castigo es por IP: un vecino en la misma oficina no paga el error.
    comprobar("otra IP no hereda el bloqueo", f.bloqueado("10.0.0.8") == 0)
    for _ in range(6):
        f.fallo("10.0.0.8")
    comprobar("cada IP se bloquea por su cuenta",
              f.bloqueado("10.0.0.8") > 0 and f.bloqueado(ip) > 0)

    # Pasado el castigo se vuelve a poder intentar.
    f.avanzar(299)
    comprobar("un segundo antes del final sigue bloqueada", f.bloqueado(ip) > 0)
    f.avanzar(2)
    comprobar("pasado el bloqueo se puede volver a intentar", f.bloqueado(ip) == 0)
    comprobar("y el contador arranca de cero otra vez", ip not in f._fallos)

    # Un acierto limpia lo anterior: quien se equivoco al teclear y luego
    # entro bien no arrastra el contador toda la tarde.
    f2 = SesionesReloj(intentos_max=3, bloqueo_segundos=60)
    f2.fallo(ip)
    f2.fallo(ip)
    f2.acierto(ip)
    f2.fallo(ip)
    f2.fallo(ip)
    comprobar("un acierto limpia los fallos previos", f2.bloqueado(ip) == 0)
    f2.fallo(ip)
    comprobar("y a partir de ahi se vuelve a contar desde el acierto",
              f2.bloqueado(ip) > 0)
    f2.acierto(ip)
    comprobar("un acierto tambien levanta un bloqueo activo",
              f2.bloqueado(ip) == 0)
    f2.acierto("ip-que-nunca-fallo")
    comprobar("acertar sin fallos previos no lanza", True)

    # 8b. El contador de fallos no puede crecer sin fin. Quien prueba claves
    #     desde una IP distinta cada vez nunca llega a entrar, asi que si solo
    #     se limpiara al abrir sesion el diccionario no se vaciaria jamas.
    f3 = SesionesReloj(intentos_max=5, bloqueo_segundos=300)
    for i in range(200):
        f3.fallo(f"203.0.113.{i}")
    comprobar("los 200 intentos desde IPs distintas se anotan",
              len(f3._fallos) == 200)
    f3.avanzar(301)
    f3.fallo("203.0.113.250")  # un intento mas, sin que nadie inicie sesion
    comprobar("pasado el bloqueo los fallos viejos se tiran solos",
              len(f3._fallos) == 1 and "203.0.113.250" in f3._fallos)

    # 9. Concurrencia: el panel corre sobre ThreadingHTTPServer y cada pestana
    #    abierta pide el estado desde su propio hilo.
    c = ses.Sesiones()
    hilos_n = 20
    por_hilo = 25
    salida = threading.Barrier(hilos_n)  # que arranquen todos a la vez
    resultados = []
    resultados_lock = threading.Lock()

    def trabajar(indice):
        salida.wait()
        # Cada hilo es una cuenta distinta: asi ademas se comprueba que con
        # veinte logins simultaneos ningun token acaba apuntando a otro usuario.
        quien = f"usuario{indice}"
        mios = [c.abrir(quien) for _ in range(por_hilo)]
        validos = sum(1 for t in mios if c.valida(t) == quien)
        # La mitad cierra sesion; la otra mitad se queda abierta.
        cerrados = mios[: por_hilo // 2]
        for t in cerrados:
            c.cerrar(t)
        muertos = sum(1 for t in cerrados if not c.valida(t))
        vivos = [t for t in mios[por_hilo // 2:] if c.valida(t) == quien]
        with resultados_lock:
            resultados.append((mios, validos, muertos, vivos))

    hilos = [threading.Thread(target=trabajar, args=(i,)) for i in range(hilos_n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    todos = [t for r in resultados for t in r[0]]
    esperados = hilos_n * por_hilo
    comprobar(f"los {hilos_n} hilos terminaron", len(resultados) == hilos_n)
    comprobar(f"se abrieron las {esperados} sesiones", len(todos) == esperados)
    comprobar("ningun token se repitio entre hilos", len(set(todos)) == esperados)
    comprobar("todas las sesiones valian nada mas abrirse",
              sum(r[1] for r in resultados) == esperados)
    comprobar("todas las sesiones cerradas dejaron de valer",
              sum(r[2] for r in resultados) == hilos_n * (por_hilo // 2))
    vivos = [t for r in resultados for t in r[3]]
    comprobar("las sesiones que nadie cerro siguen vivas",
              len(vivos) == hilos_n * (por_hilo - por_hilo // 2))
    comprobar("y siguen vivas despues de que todo pare",
              all(c.valida(t) for t in vivos))
    comprobar("no queda ninguna sesion de mas ni de menos",
              len(c._abiertas) == len(vivos))
    # Echar a una cuenta con muchas sesiones abiertas de muchas cuentas: solo
    # se lleva por delante las suyas.
    suyas = por_hilo - por_hilo // 2
    comprobar("cerrar_usuario se lleva solo las sesiones de esa cuenta",
              c.cerrar_usuario("usuario0") == suyas
              and len(c._abiertas) == len(vivos) - suyas)

    # 10. El contador de fallos tambien se toca desde varios hilos a la vez:
    #     un ataque reparte los intentos entre conexiones simultaneas, y si el
    #     conteo se pierde por el camino el bloqueo no llega a saltar nunca.
    g = ses.Sesiones(intentos_max=1000, bloqueo_segundos=300)
    salida2 = threading.Barrier(hilos_n)

    def martillear(indice):
        salida2.wait()
        propia = f"192.168.1.{indice}"
        for _ in range(50):
            g.fallo("10.9.9.9")   # todos contra la misma IP
            g.fallo(propia)       # y cada uno contra la suya

    hilos = [threading.Thread(target=martillear, args=(i,)) for i in range(hilos_n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    comprobar("no se pierde ningun fallo con 20 hilos contando a la vez",
              g._fallos["10.9.9.9"][0] == hilos_n * 50)
    comprobar("cada IP conserva su propia cuenta",
              all(g._fallos[f"192.168.1.{i}"][0] == 50 for i in range(hilos_n)))
    comprobar("con 1000 fallos la IP acaba bloqueada", g.bloqueado("10.9.9.9") > 0)

    print()
    if FALLOS:
        print(f"{len(FALLOS)} prueba(s) fallaron:")
        for f in FALLOS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("Todas las pruebas pasaron.")


if __name__ == "__main__":
    main()
