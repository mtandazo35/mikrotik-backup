# mkbackup

Respaldo de configuraciones MikroTik: **texto plano versionado en git** y
**backup binario con retención**, con un **panel web multiempresa** para
mirarlo y gestionarlo.

Pensado para una flota de ~300 RouterOS en un ISP que administra equipos de
varios clientes.

---

## ⚡ Instalación en una línea

```bash
curl -fsSL https://raw.githubusercontent.com/mtandazo35/mikrotik-backup/main/install.sh | bash
```

Para **Debian y derivados** (necesita `apt-get`) y **como root**: todo vive
bajo `/root`, así que el instalador se niega si no lo eres.

Lo que hace: instala `git`, `python3` y `python3-venv`; clona el código en
`/root/mkbackup/app`; crea el entorno virtual en `/root/mkbackup/.venv`;
intenta añadir `openpyxl` (opcional, solo para importar `.xlsx`, y si falla
sigue adelante); deja `config.yaml` e `inventory.csv` en `/root/mkbackup` con
permisos `0600`; e instala y arranca las dos unidades de systemd.

**Es idempotente**: volver a lanzarlo es la forma de actualizar. Hace `fetch` y
`reset --hard` sobre el código y reinstala las unidades, y **no toca
`config.yaml`, el inventario ni los datos** si ya existen.

Al terminar imprime la **clave del panel**, generada al azar en la instalación
nueva:

```
  Panel:  http://127.0.0.1:8080/
  Usuario: admin
  Clave:   <20 caracteres al azar>

    APUNTALA AHORA: no se guarda en ningun sitio, solo su hash.
```

Apúntala en ese momento: en `config.yaml` solo queda su **hash pbkdf2**, no la
clave. Si se pierde, se cambia con `mkbackup --clave-usuario admin` (ver
[Rescate por terminal](#rescate-por-terminal)).

El script **no pide nada por teclado a propósito**: cuando llega por una
tubería, la entrada estándar *es* el propio script, y un `read` se comería las
líneas que faltan por ejecutar. De ahí que la clave se genere sola.

> Si prefieres no ejecutar un script de internet a ciegas, léelo antes
> (`curl -fsSL https://raw.githubusercontent.com/mtandazo35/mikrotik-backup/main/install.sh | less`)
> o clona el repositorio e instala a mano con la receta de abajo.

Se puede desviar con tres variables: `MKBACKUP_REPO`, `MKBACKUP_RAMA` y
`MKBACKUP_DESTINO`.

**Antes de soltarlo sobre la flota, lee [Estado](#estado).**

### Instalación manual

El código va en `app/` y los datos en la carpeta de arriba. Es el mismo reparto
que hace el instalador, y las unidades de systemd lo dan por hecho
(`WorkingDirectory=/root/mkbackup/app`). Están separados a propósito: con el
repositorio y los respaldos mezclados, un `git clean` de alguien depurando se
lleva los backups por delante.

```bash
# En el servidor (Debian 12/13), como root
mkdir -p /root/mkbackup && chmod 700 /root/mkbackup
git clone https://github.com/mtandazo35/mikrotik-backup.git /root/mkbackup/app
cd /root/mkbackup

python3 -m venv .venv
.venv/bin/pip install -r app/requirements.txt
.venv/bin/pip install openpyxl          # opcional: importar .xlsx

cp app/config.example.yaml /root/mkbackup/config.yaml
head -n 1 app/examples/inventory.csv > /root/mkbackup/inventory.csv

chmod 700 /root                          # en Debian ya viene así: compruébalo
chmod 600 /root/mkbackup/config.yaml     # lleva credenciales
chmod 600 /root/mkbackup/inventory.csv   # puede llevarlas también

# Generar la clave del panel y pegar el hash en web.clave_hash
cd app && ../.venv/bin/python -m mkbackup.cli --hash-clave
```

El inventario se crea solo con la cabecera y no con los equipos de ejemplo: si
no, el primer ciclo se pasa minutos intentando conectar con IPs que no existen
y el panel abre con una pantalla llena de fallos que no son de nadie. Los
ejemplos están en `app/examples/inventory.csv` para copiar de ahí.

Después, editar `config.yaml`, instalar las unidades (ver
[Automatizar](#automatizar-el-programador-no-un-timer)) y seguir con
[Preparar los MikroTik](#preparar-los-mikrotik).

---

## Estado

**Versión 0.1.0 — probado de punta a punta contra un simulador, no contra un
MikroTik real.**

Verificado:

- Normalización del export, detección de cambios, git, retención de binarios y
  lectura de inventarios sucios (`tests/test_limpieza.py`, `tests/test_almacen.py`)
- **Flujo completo por SSH** contra `tests/simulador_routeros.py`: conexión,
  autenticación, `/export`, limpieza, commit. Cuatro ejecuciones seguidas
  producen **dos** commits — el alta y un cambio real de DNS — y las dos
  ejecuciones sin cambios no tocan el repo
- **Alta con nombre automático**: un equipo dado de alta sin nombre responde su
  `/system identity` y entra con él; el que no responde entra con su IP y el
  primer respaldo correcto lo renombra solo, moviendo su histórico con `git mv`
- **Cuentas, roles, permisos y alcance por empresa** (`tests/test_usuarios.py`),
  incluida la invariante del último administrador total y la migración del
  archivo de usuarios del formato 1 al 2
- **Lo que se sabe de cada equipo** —modelo, versión, último respaldo bueno— y
  la regla de que un dato vacío nunca pisa uno bueno (`tests/test_hechos.py`)
- **Importación** de plantillas CSV y `.xlsx`, **diffs enmascarados** y el
  **panel** entero (login, altas, bajas, edición, historial, usuarios,
  auditoría, ajustes)
- **Intervalo por equipo**: a quién le toca, herencia del intervalo general,
  descuento del ciclo y suelo del tic (`tests/test_planificador.py`)
- Manejo de equipo inalcanzable: respeta el timeout, reintenta y sale con
  código 1 sin colgarse

Sin verificar todavía, hace falta hardware:

- El diálogo real con RouterOS 6 y 7 (prompts, rarezas por versión)
- **La descarga del `.backup` por SFTP** — el simulador no implementa SFTP, así
  que toda la ruta del binario está sin probar
- **Las credenciales por equipo contra un router real.** La resolución de qué
  usuario y qué clave se usan está probada; que RouterOS acepte esa combinación
  cuando viene del inventario y no de `config.yaml`, no
- **El modelo leído de `board-name`** en equipos reales de v6 y v7

No lo pongas a respaldar 300 equipos sin haber hecho antes un `--solo` contra
uno de verdad.

---

## Por qué SSH y no la API

La pregunta obvia es por qué no usar la API SSL (8729), que parece más moderna.
La razón es concreta:

> *"An export command can be executed from each menu ... and is available for
> the CLI only."*
> — [MikroTik, Configuration Management](https://help.mikrotik.com/docs/spaces/ROS/pages/328155/Configuration+Management)

`/export` **no existe en la API**. La API expone `print` por menú, que devuelve
datos estructurados, no el texto de configuración. Se podría reconstruir un
export recorriendo todos los menús, pero cualquier menú que se olvide es
configuración que no se respalda — y no te enteras hasta que la necesitas.

Por eso el transporte es SSH. La API SSL sí sería el transporte correcto para
otras cosas (inventario, estado, acciones masivas).

## Qué guarda, y por qué dos formatos

| | `/export` (.rsc) | `/system backup save` (.backup) |
|---|---|---|
| Formato | texto plano | binario propietario |
| Legible / versionable | sí | no |
| Passwords de usuarios del sistema | **no** | sí |
| Certificados instalados | **no** | sí |
| Claves SSH | **no** | sí |
| Secretos PPPoE / PSK / VPN | sí (con `show-sensitive`) | sí |
| Sirve para restaurar el equipo entero | parcial | sí |

`/export` es lo que quieres leer, comparar y auditar. `.backup` es lo que
quieres tener a las 2 de la mañana cuando un equipo murió. Por eso se guardan
los dos.

### El binario no va a git (a propósito)

El `.backup` cambia en **cada** ejecución aunque la configuración sea idéntica:
lleva estado interno y marcas de tiempo. Y git no sabe deltificar binarios.

Con 300 equipos × ~100 KB diarios serían ~11 GB al año de objetos irrepetibles,
y se perdería justo lo que se busca: que el repo contenga solo cambios reales.

Solución: el `.rsc` va a git, y el `.backup` a disco con retención por equipo,
regenerado únicamente cuando el texto delató un cambio de verdad.

## Cómo detecta "solo cambios"

RouterOS mete en el export líneas que cambian siempre aunque no cambie nada:

```
# 2026-07-25 10:00:00 by RouterOS 7.14     <- la hora, siempre distinta
# inactive time                            <- comentarios intermitentes
# poe-out status: short_circuit
# ether5 not ready
```

Sin filtrarlas, **cada respaldo parece un cambio**: el repo se llena de commits
vacíos y las alertas dejan de leerse por ruido. `mkbackup` las normaliza antes
de comparar (ver `limpiar_export` en `mkbackup/device.py`), y solo escribe y
commitea si el resultado difiere del anterior.

Las pruebas de `tests/test_limpieza.py` cubren esto en ambas direcciones: que el
ruido no cuente como cambio, y que un cambio real sí se detecte.

---

## Instalación, en detalle

### Todo vive bajo /root, y lo que eso cuesta

Los datos —repositorio git, binarios, inventario, `estado.json`,
`ajustes.json`, `usuarios.json`, `equipos.json`, `auditoria.log`— van a
**`/root/mkbackup`**, y las dos unidades de systemd corren con `User=root`,
`ProtectHome=false` y `ReadWritePaths=/root/mkbackup`. Es lo que se decidió
para este despliegue, y el intercambio conviene tenerlo claro:

- **A favor:** `/root` es 0700. El inventario puede llevar las contraseñas SSH
  de los routers en claro (ver más abajo), y ahí **no lo lee ningún otro usuario
  del sistema**.
- **En contra:** los dos servicios corren como root, así que un fallo en
  cualquiera de ellos se explota con **todos los privilegios de la máquina**, no
  con los de un usuario sin permisos. Por eso el endurecimiento de las unidades
  (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`,
  `ProtectKernelTunables`, `RestrictSUIDSGID`…) importa **más**, no menos: es lo
  único que queda entre un fallo aquí y el resto del sistema. El que más pesa es
  el panel: es lo único que escucha en un puerto.

`ProtectHome=false` está solo porque `ProtectHome=true` dejaría `/root`
inaccesible, que es justo donde vive todo; `ReadWritePaths=/root/mkbackup` acota
lo que se puede escribir. El panel lleva además
`RestrictAddressFamilies=AF_INET AF_INET6`, porque necesita salir a la red para
preguntarle su nombre a un router al darlo de alta.

**Bajar el privilegio es un cambio corto** si lo prefieres: crear un usuario
dedicado, mover la carpeta a un sitio suyo y ajustar `User=`, `Group=` y
`ReadWritePaths=` en **las dos unidades**, más las rutas de `config.yaml`. El
inventario tiene que quedar en un directorio que solo ese usuario pueda leer.

### Dónde va el inventario (léelo antes de instalar)

El inventario **no es de solo lectura**: lo reescriben el panel (altas,
ediciones, bajas, importación) y el renombrado automático de los equipos dados
de alta sin nombre.

Las dos unidades llevan `ProtectSystem=strict`, así que lo único escribible es
lo que declare `ReadWritePaths`. Si dejas el inventario fuera de
`/root/mkbackup`, **ni el panel ni el renombrado automático pueden guardar
nada**: el alta falla y el equipo se queda con su IP para siempre.

Por eso el inventario va en el mismo sitio que el resto de los datos:

```yaml
# /root/mkbackup/config.yaml
inventario: /root/mkbackup/inventory.csv
```

Si lo mueves a otra ruta, añádela a `ReadWritePaths` en **las dos unidades**
(`mkbackup.service` y `mkbackup-web.service`), no solo en una: lo escriben los
dos procesos.

### Preparar los MikroTik

```
/user group add name=backup-readonly \
    policy=ssh,read,test,sensitive,ftp,!write,!policy,!reboot

/user add name=Respaldo group=backup-readonly \
    address=<IP_DEL_SERVIDOR>/32 password=<clave>
```

Dos policies que suelen olvidarse:

- **`sensitive`** — sin ella, `/export` censura passwords y PSK con
  `[FILTERED]` y el respaldo no sirve para restaurar credenciales.
- **`ftp`** — necesaria para descargar el `.backup` por SFTP. Sin ella, el
  export funciona pero el binario falla.

`address=` restringe el usuario a la IP del servidor de respaldo: aunque la
clave se filtre, no sirve desde otro sitio.

Esas credenciales generales se ponen en la sección `ssh:` de `config.yaml` o
desde **Ajustes** en el panel. Si un cliente no deja crear este usuario en sus
routers, esos equipos pueden traer las suyas en el inventario (columnas
`usuario` y `clave`, ver [Credenciales por equipo](#credenciales-por-equipo)).

### Probar antes de soltar la flota

```bash
# 1. Validar configuración e inventario sin tocar ningún equipo
.venv/bin/python -m mkbackup.cli -c /root/mkbackup/config.yaml --probar-config

# 2. Un solo equipo, con log detallado
.venv/bin/python -m mkbackup.cli -c /root/mkbackup/config.yaml \
    --solo BTS-Norte-01 -v

# 3. Toda la flota, un ciclo y termina
.venv/bin/python -m mkbackup.cli -c /root/mkbackup/config.yaml
```

### Automatizar: el programador, no un timer

`mkbackup.service` es un **proceso residente** (`mkbackup --planificador`) que
vive siempre y dispara él los ciclos: cada `planificador.intervalo_minutos`, o
antes si algún equipo lleva un intervalo propio más corto en el inventario.
**No hay `mkbackup.timer`.**

```bash
cp systemd/mkbackup.service /etc/systemd/system/
cp systemd/mkbackup-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mkbackup mkbackup-web
systemctl status mkbackup mkbackup-web
```

El panel **no arranca sin login**: hace falta `web.clave_hash` en el
`config.yaml`, o un `usuarios.json` con al menos una cuenta.

**Si vienes de una versión con timer**, hay que quitarlo a mano o seguirás
lanzando ciclos por duplicado:

```bash
systemctl disable --now mkbackup.timer
rm /etc/systemd/system/mkbackup.timer
cp systemd/mkbackup.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mkbackup
```

El motivo del cambio es concreto: el intervalo se cambia desde el panel, y con
un timer eso significaría que el panel **reescribe una unidad de systemd y llama
a `daemon-reload`** cada vez que alguien mueve un número en un formulario. Con
el programador, cambiar el intervalo es escribir un JSON diminuto en
`/root/mkbackup`: no se toca ninguna unidad y no se reinicia nada.

Y sí, en este despliegue el panel corre como root de todas formas (ver
[Todo vive bajo /root](#todo-vive-bajo-root-y-lo-que-eso-cuesta)); que **pueda**
no es motivo para que lo haga. Lo que el panel sabe reescribir es lo único que
tiene que reescribir.

---

## Inventario

CSV con cabecera. Solo `nombre` e `ip` son obligatorias; `empresa`, `puerto`,
`grupo`, `intervalo_minutos`, `usuario` y `clave` son opcionales:

```csv
nombre,empresa,ip,puerto,grupo,intervalo_minutos,usuario,clave
Router-Core-Matriz,Andinanet Telecomunicaciones,10.20.0.1,22,core,60,,
BTS-Norte-01,Andinanet Telecomunicaciones,10.20.30.11,22,bts,,,
BTS-Sur-02,Andinanet Telecomunicaciones,10.20.30.12,2288,bts,,,
CCR-Borde-Quito,Fibra Austral S.A.,192.168.88.1,,quito,1440,usuario-de-ejemplo,cambiame
```

El lector tolera lo que sale de Excel (CRLF, BOM, espacios) y **avisa de cada
línea que corrige o descarta**: nombres duplicados que se pisarían, puertos
fuera de rango, y el error clásico de apuntar a 8291 (Winbox) o 8728/8729 (API)
en vez de al puerto SSH.

Los inventarios escritos antes de que existieran las tres últimas columnas
**siguen cargando**: esas filas quedan con el intervalo general y con las
credenciales generales. Al escribir, el CSV sale siempre con la cabecera
completa.

### Cada equipo puede llevar su propio ritmo

`intervalo_minutos` es cada cuánto se consulta **ese** equipo. Vacío o `0` =
usar el intervalo general de Ajustes (`planificador.intervalo_minutos`); un
número mayor que cero **manda** sobre él.

Existe porque un router de sucursal que no cambia en meses no necesita el mismo
ritmo que un core: consultarlo menos le quita trabajo a él —cada respaldo es una
sesión SSH y un `/export` completo— y a la red. Al revés también: un core se
puede poner a 30 minutos sin bajarle el intervalo a los otros 299 equipos.

El programador **no respalda la flota entera en cada ciclo**: mira a quién le
toca y consulta solo a esos, y **si no le toca a nadie no lanza ciclo** (ni
estado de "0 equipos", ni git tocado para nada). Ver `equipos_pendientes` y
`toca_ahora` en `mkbackup/planificador.py`.

Por debajo de **5 minutos** `cargar` avisa —no lo prohíbe, es tu red—: a ese
ritmo cada respaldo abre una sesión SSH y pide un export entero, y en un equipo
cargado se nota. Un valor no numérico o negativo tampoco descarta la fila: se
degrada al intervalo general y se avisa, porque perder un router del respaldo
por un intervalo mal escrito sería mucho peor.

El campo se rellena igual desde el formulario del panel y desde la plantilla de
Excel. En el formulario sí se **rechaza** un intervalo mal escrito en vez de
degradarlo: hay alguien delante que puede corregirlo, y aceptarlo en silencio le
haría creer que su router se consulta cada hora cuando seguiría con el general.

### Credenciales por equipo

`usuario` y `clave` son las credenciales SSH de **ese** equipo. Vacías —el caso
normal— significan "usa las generales de `ssh:` en `config.yaml`". Existen
porque un ISP no entra a todos sus equipos con el mismo usuario: hay clientes
que imponen las suyas en sus routers.

Se deciden **campo a campo** (`_credenciales` en `mkbackup/device.py`), así que
un equipo puede traer solo el usuario y heredar la clave:

| Qué se usa | Precedencia |
|---|---|
| usuario | `equipo.usuario` si no está vacío, si no `ssh.usuario` |
| password | `equipo.clave` si no está vacía, si no `ssh.password` |
| clave privada | `ssh.clave_privada`, **pero se descarta si el equipo trae clave propia** |

Lo último es la parte que hay que pensar. Si se pasaran las dos, paramiko
intentaría primero la publickey y la contraseña del equipo solo se usaría si la
llave falla; en el caso malo —la llave global también vale para ese router—
entraríamos con una identidad distinta de la que pidió el cliente y la
credencial del inventario quedaría puesta pero muerta, sin que nadie se enterara
hasta que el cliente rotara la llave. Una credencial escrita a mano para un
equipo concreto es la intención más explícita que hay: gana, y para ese equipo
se entra **solo por contraseña**.

Rellenar solo uno de los dos campos no es error (puede ser a propósito: usuario
propio con la clave de siempre), pero `cargar` lo avisa, porque casi siempre es
un descuido al llenar el Excel.

> **Las claves se guardan en claro en el inventario. No hay cifrado.** Por eso
> el archivo vive en `/root` y `mkbackup` lo escribe con permisos `0600`, y por
> eso **cualquier copia de ese archivo se lleva el acceso a toda la red**: un
> `scp` a un portátil, un adjunto en un correo, un respaldo del servidor
> completo o un `git add` sin mirar. Si hay que moverlo, trátalo como tratarías
> la contraseña del router más crítico, y bórralo cuando termines.

La clave **nunca se manda al navegador**: al editar un equipo, el campo del
formulario aparece vacío, y vacío significa "conserva la que ya tiene" (si no,
abrir el formulario y guardar sin tocar nada le borraría la contraseña). Y si
rellenas esas columnas en la plantilla de Excel, **ese archivo también lleva
contraseñas**: bórralo del disco en cuanto termines de importar.

### La empresa organiza el repositorio

Las rutas dentro del repo son **`empresa/equipo.rsc`**: cada empresa tiene su
carpeta y dentro van todos sus equipos.

```
configs.git/andinanet-telecomunicaciones/BTS-Norte-01.rsc
configs.git/fibra-austral-s.a/CCR-Borde-Quito.rsc
```

Cada cliente queda aislado en su carpeta, y su histórico completo se saca de un
tirón con `git log -- <empresa>/` sin tener que acordarse de qué equipos son
suyos. Al abrir la carpeta de un cliente se ven **todos** sus respaldos, sin
entrar en una subcarpeta por grupo a buscar un router del que ya nadie recuerda
si se clasificó como `core` o como `bts`.

**El `grupo` sigue existiendo** en el inventario y en el panel: clasifica y
filtra. Lo que no hace es decidir dónde se guarda el archivo. Efecto útil de
eso: **reclasificar un equipo de grupo no mueve su archivo ni parte su histórico
de git.** Solo lo mueven el cambio de nombre y el de empresa, y ahí el
movimiento se hace con `git mv`.

El árbol de binarios copia el mismo esquema (`binarios/empresa/equipo/`).

El nombre legible se guarda **tal cual se escribe** ("Fibra Austral S.A."), y
para la carpeta se usa un slug en minúsculas y sin tildes (ver `sanear_empresa`
en `mkbackup/inventory.py`). No es capricho: los espacios complican los comandos
de git, las tildes se codifican distinto según el sistema de archivos (NFC en
Linux, NFD en macOS) y el mismo respaldo acabaría en dos carpetas según quién
clone el repo. Si dos empresas distintas producen el mismo slug, `cargar` avisa:
sus respaldos se mezclarían en una sola carpeta.

Ese slug es además la unidad con la que se compara el **alcance** de una cuenta
del panel (ver [Multitenant](#multitenant-quién-ve-qué-equipos)): es la
granularidad a la que los datos están de verdad separados en disco.

Los inventarios **sin la columna `empresa` siguen cargando**: esas filas caen
en `sin-empresa` y nada se rompe.

---

## Uso diario

```bash
# Ver qué cambió y cuándo
git -C /root/mkbackup/configs.git log --oneline

# Todo el histórico de un cliente
git -C /root/mkbackup/configs.git log -- andinanet-telecomunicaciones/

# Qué cambió exactamente en un equipo
git -C /root/mkbackup/configs.git log -p -- andinanet-telecomunicaciones/BTS-Norte-01.rsc

# Buscar en todas las configuraciones a la vez
git -C /root/mkbackup/configs.git grep "8.8.8.8"

# Recuperar cómo estaba un equipo hace 3 commits
git -C /root/mkbackup/configs.git show HEAD~3:andinanet-telecomunicaciones/BTS-Norte-01.rsc
```

Lo mismo, sin consola, está en el panel: `/cambios`, `/historial` y
`/diferencia` (ver más abajo).

### Restaurar un equipo

```bash
# Configuración (texto) — requiere el equipo accesible
scp andinanet-telecomunicaciones/BTS-Norte-01.rsc Respaldo@10.20.30.11:
ssh Respaldo@10.20.30.11 "/import BTS-Norte-01.rsc"

# Equipo entero (binario) — incluye certificados y claves
scp BTS-Norte-01_20260725_120000_123.backup admin@10.20.30.11:
ssh admin@10.20.30.11 "/system backup load name=BTS-Norte-01_20260725_120000_123"
```

Restaura con la **misma versión de RouterOS** con la que se hizo el respaldo:
comandos que existen en una versión pueden no existir en otra. **El panel no
restaura nada**: esto es a mano, y a propósito.

---

## Panel web

El respaldo corre desatendido y lo único que deja es el journal. Con 300
equipos, un ciclo dura lo suficiente como para que alguien pregunte si está
corriendo; y el inventario lo mantiene gente que no va a editar un CSV por SSH.

El panel cubre eso, en un proceso aparte del respaldo: **ver cómo va el ciclo**,
**gestionar la flota** (altas, importación, historial, cada cuánto se respalda)
y **repartir quién ve qué** con cuentas, roles y alcance por empresa. Lo que
sigue sin hacer, y por qué, está al final de esta sección.

```bash
.venv/bin/python -m mkbackup.cli -c /root/mkbackup/config.yaml --web
# http://127.0.0.1:8080/
```

La navegación tiene seis secciones —Estado, Equipos, Cambios, Usuarios,
Auditoría, Ajustes— y **cada una aparece solo si la cuenta tiene su permiso**.
Esconder el enlace no protege nada, así que además **cada ruta lo comprueba en
el servidor**: cualquiera puede escribir la URL a mano.

### Primero la clave: el panel no arranca sin login

La lista de nombres, IPs, empresas y grupos de toda la flota es un mapa de la
red. Por eso pide usuario y contraseña, y por eso **`servir()` se niega a
arrancar** (sale con código 2 y lo dice en el log) si falta `web.clave_hash` o
si no hay ninguna cuenta, en vez de quedarse abierto de par en par por un
descuido de configuración.

La clave no se escribe en el YAML. Se genera su hash:

```bash
.venv/bin/python -m mkbackup.cli --hash-clave
# Clave para el panel:
# Repitela:
#
# Pega esto en la seccion web: de tu config.yaml
#
#   clave_hash: pbkdf2_sha256$240000$...
```

La pide dos veces, exige **8 caracteres como mínimo** y no guarda nada: solo
imprime la línea para pegarla en `web:` de `/root/mkbackup/config.yaml`, que es
de donde saldrá la cuenta inicial la primera vez que arranque el panel. A partir
de ahí, las claves se cambian **desde el propio panel** (cada cuenta la suya en
`/cuenta`, pidiendo la actual) o con `--clave-usuario`.

| Opción de `web:` | Por defecto | Qué hace |
|---|---|---|
| `usuario` | `admin` | La cuenta que se siembra en el archivo de usuarios la primera vez. Después manda ese archivo. |
| `clave_hash` | *(vacío)* | El hash de `--hash-clave`. Vacío = el panel no arranca. |
| `sesion_horas` | `8` | Cuánto dura la sesión antes de volver a pedir la clave. |
| `intentos_max` | `5` | Fallos seguidos desde una IP antes de frenarla. |
| `bloqueo_segundos` | `300` | Lo que espera esa IP tras pasarse de intentos. |
| `eventos_por_pagina` | `30` | Eventos por página en la auditoría. |
| `fondo_login` | *(vacío)* | Imagen de la pantalla de entrada. Se sube desde Ajustes. |

El bloqueo es **por IP** y en memoria: cinco intentos fallidos y esa dirección
queda cinco minutos fuera, con la respuesta 429 diciendo cuánto le falta. Un
acierto borra su contador, y desde `/auditoria` se puede levantar a mano.

La sesión es una cookie `HttpOnly`, `SameSite=Strict` y con `Max-Age` igual a
`sesion_horas`; el token es opaco y vive en memoria del proceso, no firmado. El
precio de eso es que **reiniciar el panel cierra las sesiones abiertas**; a
cambio, "Salir" —y sobre todo echar a alguien— las cierra de verdad y no queda
un token válido dando vueltas. `SameSite=Strict` es además lo que protege las
altas y las bajas de un CSRF: un POST desde otra página llega sin cookie y muere
en el login.

La cuenta de cada petición **se resuelve del archivo, no del token**: es más
trabajo por petición, pero significa que bajarle el rol a alguien surte efecto
en la siguiente página que pida, y que una cuenta borrada deja de valer aunque
su token siguiera vivo.

### Cuentas, roles y permisos

Las cuentas **no viven en `config.yaml`**: viven en su propio archivo JSON
(`almacen.usuarios`, por defecto `/root/mkbackup/usuarios.json`), escrito de
forma atómica y con permisos `0600`. Guarda **hashes pbkdf2-sha256 con sal**,
nunca contraseñas: quien se lleve una copia del archivo no se lleva las claves
de nadie.

Va aparte porque `config.yaml` lo edita **una persona con root** —lleva
comentarios, decisiones y las credenciales de conexión— y el servicio web no
puede ni debe reescribirlo. Las cuentas, en cambio, se crean y se cambian desde
la web, así que necesitan un archivo que el servicio pueda escribir.

**Migración automática:** quien ya tenía el panel funcionando con un único
usuario en `web.usuario` / `web.clave_hash` se encuentra su misma cuenta y su
misma clave la primera vez que arranca el panel nuevo (`sembrar`), con el rol
`admin` y acceso a todo. Sin eso, actualizar dejaría a la gente fuera de su
propio panel. A partir de ahí manda el archivo de usuarios.

Hay **ocho permisos** (`PERMISOS` y `ETIQUETAS_PERMISO` en
`mkbackup/usuarios.py`). Es una lista cerrada a propósito: un permiso que solo
existiera en el archivo de un cliente no lo comprobaría nadie.

| Permiso | Qué abre |
|---|---|
| `ver` | Ver equipos, estado y cambios |
| `diferencias` | Ver el contenido de los cambios (diffs) |
| `equipos.crear` | Añadir equipos |
| `equipos.editar` | Editar equipos |
| `equipos.baja` | Dar de baja equipos |
| `equipos.importar` | Importar desde Excel |
| `ajustes` | Cambiar los ajustes del programador |
| `usuarios` | Gestionar usuarios y roles (y ver la auditoría) |

**Los roles son plantillas editables**, no constantes del código: se crean, se
editan y se borran desde el panel. Una instalación nueva arranca con `admin`,
`operador` y `lector`, y a partir de ahí son datos — un ISP no tiene los mismos
perfiles que otro, y pedir un despliegue nuevo para añadir "facturación" no
tiene sentido. El rol `admin` **no se puede borrar** (es el punto de partida
conocido para recuperar el control) y un rol que alguien tenga puesto tampoco.

Encima del rol hay **excepciones por usuario**, porque siempre aparece el caso
de "este de aquí, y solo este, también importa el Excel":

```
permisos efectivos = (permisos del rol | permisos_mas) - permisos_menos
```

**Quitar gana siempre.** Si un permiso está a la vez en `mas` y en `menos`, se
queda fuera: ante una duda —o ante una interfaz que mande las dos listas mal— el
error tiene que caer del lado restrictivo. En el formulario se marcan casillas y
el panel guarda la **diferencia contra el rol**, no la lista suelta: así, si
mañana se le añade un permiso al rol, lo hereda quien no lo tenga tocado a mano.

Cambiarle a alguien el rol, los permisos, el alcance o la clave **cierra sus
sesiones abiertas**. Si no, seguiría dentro con lo de antes hasta que le
caducara, que es justo el rato en el que hace falta que no lo esté.

#### La invariante: el último administrador total

> **Siempre tiene que quedar al menos un usuario que tenga a la vez el permiso
> `usuarios` y alcance a todo.**

Ese es el único que puede volver a repartirlo todo: crear cuentas, arreglar
roles y dar acceso a cualquier empresa. Sin ninguno, el panel se queda sin
llaves y la única salida es editar el JSON a mano por SSH.

Se protege en **todas** las operaciones que podrían romperla, incluidas las
indirectas: borrar esa cuenta, cambiarle el rol, quitarle el permiso con
`permisos_menos`, recortarle el alcance, **quitarle el permiso `usuarios` al rol
que se lo daba**, o borrar ese rol. No se mira "qué campo cambió": se calcula
cómo quedaría el sistema entero y se pregunta si sigue habiendo llaves.

Un detalle deliberado: si el archivo ya llegó sin ningún administrador total
(alguien lo editó a mano y se equivocó), la regla **no** bloquea nada. Congelar
el panel entonces sería castigar dos veces por el mismo error.

Una cuenta nueva se crea **sin alcance**: no ve ningún equipo hasta que alguien
diga cuáles. La única excepción es la **primera** cuenta del archivo, que recibe
alcance total porque alguien tiene que quedarse las llaves.

#### Rescate por terminal

Tres opciones del CLI para cuando no se puede entrar al panel:

```bash
.venv/bin/python -m mkbackup.cli -c config.yaml --listar-usuarios
.venv/bin/python -m mkbackup.cli -c config.yaml --crear-usuario ana --rol admin
.venv/bin/python -m mkbackup.cli -c config.yaml --clave-usuario admin
```

- `--listar-usuarios` — nombre, rol y último acceso de cada cuenta. Es lo
  primero que se mira cuando nadie sabe qué cuentas hay.
- `--crear-usuario` — el arranque: la primera cuenta de una instalación, o una
  de repuesto si se borró la última con permiso de usuarios. Pide la clave por
  terminal (mínimo 8 caracteres) y no la escribe en ningún sitio.
- `--clave-usuario` — el rescate: el único administrador olvidó su clave. Pide
  la nueva por terminal.

Las sesiones abiertas viven en la memoria del proceso del panel, que es otro:
tras un `--clave-usuario` hay que reiniciar el panel para cerrarlas.

### Multitenant: quién ve qué equipos

El rol dice **qué puede hacer** una cuenta; el **alcance** dice **sobre qué
equipos**. Las dos preguntas hay que responderlas siempre: un operador con
todos los permisos pero con alcance solo a Acme es el técnico de Acme; el mismo
rol con alcance a todo es el técnico del ISP.

El alcance se da de tres formas, y con **nivel `ver` o `editar`** (editar
incluye ver):

| Forma | Para qué |
|---|---|
| `todo` | El personal del ISP. Manda sobre cualquier otra cosa. |
| Por **empresa** | El caso normal: un cliente ve sus routers y nada más. |
| Por **equipo suelto** | El caso raro que siempre aparece. |

**Un permiso sobre un equipo SUMA al de su empresa y nunca resta.** Se toma el
mayor de los dos. Sirve para enseñar un router de una empresa que por lo demás
no se ve, o para poder editar uno concreto de una empresa que solo se mira. Lo
que *no* hace es bajar de nivel: listar un equipo con `ver` dentro de una
empresa con `editar` no lo deja en solo lectura — para eso hay que quitarle
`editar` a la empresa y listarlos uno a uno, que es lo explícito.

Las empresas se comparan por su **slug** (`sanear_empresa`), no por el texto
crudo: "Acme S.A.", "ACME S.A." y "  Acme  S.A. " son la misma empresa, y las
tildes se normalizan. Los nombres de equipo se comparan sin distinguir
mayúsculas, porque son nombres de archivo dentro del repo.

**Todo lo que devuelve datos de equipos se filtra**, no solo la tabla:

- **Los totales y las gráficas del panel.** Los contadores del archivo de estado
  son de la flota entera; se **recalculan** sobre lo que corresponde a la
  cuenta. Un cliente que ve 12 equipos pero un total de 300 ya sabe el tamaño de
  su proveedor. El recorte usa **lista blanca**: solo salen los campos que se
  nombran, así que un campo nuevo en el estado no se publica por descuido.
- **El estado del programador.** Se recorta a la última consulta de *sus*
  equipos; el tamaño del último lote y los nombres ajenos no salen.
- **`/cambios`, `/historial` y `/diferencia`.** `/diferencia` traduce la ruta
  del repositorio al equipo del inventario antes de decidir: sin eso, escribir a
  mano la ruta de otra empresa enseñaría su configuración entera. Un equipo dado
  de baja ya no está en el inventario y no se le puede calcular el alcance: solo
  lo ve quien lo ve todo.
- **Los avisos del inventario.** Nombran equipos de toda la flota (líneas
  descartadas, duplicados, choques de empresa) y además delatan que hay más.

Al **crear o editar** un equipo se comprueba el alcance sobre el equipo **ya
validado**, no sobre lo que llegó en el formulario, y cambiarle la empresa exige
poder escribir en las dos: si no, sería una forma de regalarle un equipo a
alguien — o de robárselo.

> **Cuando algo queda fuera del alcance, la respuesta es 404 y no 403.**
> Decir "no tienes permiso" sobre un nombre concreto ya confirma que ese equipo
> existe, y en multitenant eso es información del cliente de al lado. Un 403 se
> reserva para cuando el equipo **sí** se ve pero no se puede editar, donde ya
> no hay nada que revelar.

### Auditoría

El log del proceso (journald) está para diagnosticar: se rota y se mezcla con el
ruido de los respaldos. La auditoría está para responder **quién** y **cuándo**,
que es la pregunta de después de un incidente. Por eso es un archivo aparte:
`almacen.auditoria`, por defecto `/root/mkbackup/auditoria.log`, con permisos
`0600` y **una línea JSON por evento** (fecha UTC, evento, usuario, IP,
detalle), para poder filtrarla sin analizar texto libre.

Se guardan los accesos (`login_ok`, `login_fallido`, `login_bloqueado`,
`salir`, `desbloqueo`, `clave_propia`), los intentos rechazados (`sin_permiso`,
`fuera_de_alcance`) y los cambios: cuentas (`usuario_alta`, `usuario_cambio`,
`usuario_baja`), roles (`rol_cambio`, `rol_baja`), inventario (`equipo_alta`,
`equipo_cambio`, `equipo_baja`, `importacion`) y configuración (`ajustes`,
`acceso_ssh`).

**Nunca guarda claves**, ni hashes, ni tokens de sesión. De un cambio de
credenciales SSH se anota que cambió y quién, no el valor. Sí se anota el nombre
**tecleado** en un login fallido aunque no exista ninguna cuenta así: media
auditoría de un ataque es saber qué cuentas estuvieron probando. Cada valor se
recorta a una línea sin caracteres de control — si no, quien teclee un salto de
línea en el formulario puede inventarse líneas enteras del registro.

El archivo **se rota solo** al llegar a 5 MB (del orden de 30.000 eventos) y
conserva una generación anterior en `.log.1`. Quien necesite historia larga que
se lo lleve a donde guarde los logs de verdad.

La pantalla `/auditoria` —mismo permiso que gestionar cuentas: quien puede crear
usuarios es quien tiene que poder ver qué se ha hecho con ellos— filtra por tipo
de evento, por usuario y por "solo sospechosos", con paginación de
`web.eventos_por_pagina` eventos (30 por defecto). Si se pide una página que no
existe se enseña la última completa, y el título dice el rango real en vez de
mentir. Los intentos fallidos y los bloqueos van en rojo.

Arriba hay una tabla de **direcciones con intentos fallidos**, con cuántos lleva
cada una y cuánto le queda de bloqueo, y un botón **Desbloquear** para el caso
del despiste — cinco errores de teclado no tienen por qué costar cinco minutos.
Se listan también las que **aún no** llegaron al límite: ver "3 de 5 intentos"
es lo que permite darse cuenta de que algo pasa antes de que salte el bloqueo.

> Esa lista **vive en la memoria del panel**: reiniciarlo la vacía entera. Es el
> mismo precio que pagan las sesiones (ver abajo), y en un servicio de consulta
> sale a cuenta.

### Lo que se sabe de cada equipo

Además del inventario —que es lo que alguien **configuró**— el panel enseña lo
**observado**: lo que el propio RouterOS contó de sí mismo. Vive en
`almacen.hechos` (`/root/mkbackup/equipos.json`), lo escribe el respaldo y lo
lee el panel, nunca al revés.

| Dato | De dónde sale |
|---|---|
| **Modelo** | `board-name` del `/system resource print` que **ya se ejecuta**. Si viniera recortado, la cabecera `# model = ...` del `/export` que ya se descargó. |
| **Versión de RouterOS** | `version:` del mismo `/system resource print`. |
| **Último respaldo bueno** | La fecha del último ciclo que salió bien para ese equipo. |
| **Último intento** | La fecha del último ciclo que lo tocó, saliera bien o no. |

**Ni un comando ni una sesión SSH de más.** Ese `resource print` ya se hacía
para saber la versión, que es lo que decide si el export se pide con
`show-sensitive` (solo existe en v7). Manda `board-name` y la cabecera del
export queda como respaldo: `board-name` es un campo del menú `/system resource`
que existe en v6 y en v7, mientras que `# model =` es un comentario que se
añadió en las v7 y que además pasa por `limpiar_export`, donde un patrón de
ruido nuevo podría llevárselo por delante sin que nadie lo note.

> **Un dato vacío nunca pisa uno bueno.** Si el ciclo de hoy falló y no se pudo
> leer el modelo, se conserva el que ya se sabía: un equipo apagado no puede
> "olvidar" que es un RB4011, porque justo cuando algo lleva fallando es cuando
> hace falta saber a qué aparato hay que ir.

Por eso son dos fechas y no una. `ultimo_intento` se mueve siempre;
`ultimo_ok` solo cuando el respaldo salió bien. Eso es lo que permite decir "se
intentó hace 5 minutos pero el último bueno fue anteayer": si `ultimo_ok` se
moviera en cada intento, un equipo muerto parecería recién respaldado. En la
tabla, un equipo con fecha buena pero con el último intento fallido sale marcado
como **fallando**, porque "respaldado el martes" con el equipo caído desde el
miércoles es justo lo que engaña.

Esto no se puede deducir de git: un equipo sin cambios no deja commit, así que
el último commit de su archivo puede ser de hace meses aunque se esté
respaldando cada cuatro horas sin fallar una vez. Y tampoco sale de
`estado.json`, que solo habla del ciclo en curso — con intervalos por equipo, un
router de 24 h no aparece en la mayoría de los ciclos.

### Lo que se ve de un vistazo

La pantalla de estado muestra en qué situación está la ejecución (en curso con
barra de avance, terminada, terminada con fallos o interrumpida), el detalle
equipo por equipo, los avisos del inventario y las últimas ejecuciones.

Arriba hay una fila de totales — **Clientes, Equipos, Respaldados, Fallidos,
Próximo ciclo** — y debajo dos gráficas de tarta con leyenda y conteos:

| Gráfica | Qué reparte |
|---|---|
| **Resultado del último respaldo** | Sin cambios / Con cambios / Fallidos / Sin respaldar. |
| **Equipos por cliente** | Los cuatro clientes con más equipos, y el resto plegado en "Otros". |

Los totales se cuentan sobre los **equipos** y no sobre los contadores del
ciclo: con intervalos por equipo, un ciclo puede tocar solo a una parte de la
flota, y "3 de 3" no dice nada de los otros 297. Y todo esto se recorta al
alcance de quien mira (ver [Multitenant](#multitenant-quién-ve-qué-equipos)).

Se dibujan en **SVG generado en el navegador, sin ninguna librería externa** —
el proyecto no añade dependencias para pintar dos tartas, y un panel que corre
en una red de gestión no debería depender de un CDN. Los colores están elegidos
para distinguirse también con daltonismo, en tema claro y oscuro; se leen del
CSS y se repintan si el sistema cambia de tema con la pestaña abierta. Por eso
"Equipos por cliente" corta en cuatro más "Otros": pasado ese punto los sectores
dejan de distinguirse, y la tabla de abajo tiene el detalle completo.

### Filtros, orden y columnas

`/equipos` y `/cambios` llevan una fila de **filtros** por empresa, grupo y
nombre o IP. El buscador libre mira nombre e IP a la vez: quien escribe `10.20.`
busca una subred y quien escribe `BTS` busca por nombre, y obligarle a elegir el
campo solo estorba.

La tabla de equipos se **ordena por columna** (nombre, empresa, IP, puerto,
grupo y "cada"), pulsando la cabecera. Dos detalles:

- **Las IP se ordenan por su valor, no como texto.** Como texto, `10.20.0.9`
  iría después de `10.20.0.10`, que es justo lo que nadie espera. Los nombres
  DNS no son números: van detrás de todas las IP y entre ellos por texto.
- **Todas las claves desempatan por el nombre del equipo.** Sin eso, al ordenar
  por empresa los equipos de una misma empresa saldrían en el orden en que estén
  en el archivo, que es arbitrario — justo lo que se quiere evitar al pulsar
  "ordenar".

Un intervalo `0` no se ordena como cero: significa "hereda el general" y se va
al final, que es donde menos molesta cuando lo que buscas son los que tienen uno
propio.

También se **elige qué columnas se ven**. Están disponibles equipo, empresa, IP,
puerto, grupo, modelo, RouterOS, último respaldo, cada y usuario; por defecto se
ven todas menos puerto y usuario, que casi siempre traen el valor de siempre y
ocupan sitio para no decir nada. Lo que se elige es **cuáles**, no en qué orden:
una tabla que baraja sus columnas según quién la mire deja de ser reconocible.
La selección viaja en la URL y se conserva al filtrar y al ordenar; una columna
inventada a mano en la URL se descarta.

### La pantalla de entrada tiene fondo

Desde **Ajustes** se sube la imagen que se ve detrás del formulario de login.
Se acepta **JPG, PNG, WebP y AVIF**, hasta 4 MB.

- **Se comprueba el contenido, no la extensión.** El tipo se decide por los
  primeros bytes del archivo (firmas de JPEG y PNG, contenedor `RIFF/WEBP` y
  `ftyp/avif`): renombrar un `.html` a `.jpg` es gratis, y servir un documento
  desde el origen del panel no lo es.
- **No se acepta SVG**, aunque sea una imagen: un SVG puede llevar `<script>`
  dentro, y `/fondo` se sirve en el mismo origen que el panel, así que abrir esa
  URL ejecutaría ese código con la sesión de quien la abra. Un formato de imagen
  que además es un documento ejecutable no cabe aquí.
- **El nombre del archivo lo decide el panel**, no quien sube: se guarda como
  `fondo-login.<ext>` junto a `ajustes.json`, así que no hay forma de escribir
  fuera de la carpeta de datos ni de pisar otra cosa. Al cambiar de formato se
  borra el fondo anterior.
- **Se sirve sin pedir login**, y es por necesidad: es el fondo de la propia
  pantalla de entrada, donde todavía no hay sesión. Solo se sirve el archivo que
  diga la configuración —no hay forma de pedir otro desde la URL— pero
  **cualquiera que llegue al panel puede verla**: no pongas ahí nada que no
  quieras enseñar. La página lo dice en pantalla.

La URL lleva una marca (tamaño y fecha del archivo) que cambia cuando cambia la
imagen: así el navegador la cachea 24 horas y aun así se ve la nueva en cuanto
se sube otra. Es la única respuesta del panel que no va con `no-store`.

### Gestión de equipos desde el panel

`/equipos` es el inventario: altas, edición y bajas, con validación campo a
campo (`validar_equipo`). Aquí se **rechaza** lo que la carga del CSV solo
avisa — un grupo con caracteres raros, un nombre duplicado, una IP que no lo
es — porque hay alguien delante que puede corregirlo en el momento, mientras
que la carga corre de madrugada y prefiere degradar antes que perder un equipo
del respaldo.

El formulario incluye también el intervalo propio del equipo y sus credenciales
SSH; los tres pueden quedar vacíos, que es lo normal (ver
[Inventario](#inventario)).

Tres decisiones que conviene conocer:

- **Dar de baja no borra los respaldos.** Retirar un equipo es dejar de
  consultarlo, no perder lo que se sabía de él: sus `.rsc` siguen en el repo
  para el día que alguien pregunte cómo estaba configurado antes de retirarlo.
- **Si cambia el nombre o la empresa, cambia la ruta en el repo**, y el archivo
  se mueve con `git mv`. Si no, el siguiente respaldo crearía uno nuevo y el
  histórico del equipo quedaría partido en dos justo donde interesa seguirlo. Si
  el movimiento falla, el panel lo dice en el aviso y en el log en vez de
  dejarlo pasar. **Cambiar el grupo no mueve nada**: no entra en la ruta.
- **La clave del equipo no se manda al navegador.** Al editar aparece vacía, y
  vacía significa "conserva la que ya tiene".

| Opción | Por defecto | Qué hace |
|---|---|---|
| `editar_inventario` | `true` | En `false` el panel vuelve a ser de **solo lectura**: sin altas, sin bajas, sin importación (403), y el inventario se toca únicamente por archivo. |

Son dos condiciones distintas y los mensajes las distinguen: que el panel lo
permita (configuración) y que la cuenta tenga el permiso. "No tienes permiso"
cuando en realidad está apagado para todo el mundo manda a buscar el problema al
sitio equivocado.

Las escrituras van con candado: `inventory.guardar` es atómico, pero dos altas
simultáneas leerían la misma lista y la segunda se llevaría por delante a la
primera. Con `ThreadingHTTPServer` eso no es hipotético — cada pestaña es un
hilo.

### Nombre automático del router

En el alta, el nombre **se puede dejar vacío**. Entonces el panel le pregunta al
propio equipo su `/system identity` y lo usa. Quien da de alta los routers de un
cliente tiene a mano la lista de IPs, no los nombres exactos; obligarle a
inventárselos solo produce inventarios que no coinciden con la realidad.

Si el equipo no responde (apagado, otra clave, timeout), el alta **no falla**:
se guarda con la IP como nombre provisional. La convención del proyecto es que
**`nombre == ip` significa "pendiente de identificar"**, y el panel lo marca en
la lista. El **primer respaldo que salga bien lo renombra solo**, con `git mv`
para conservar su histórico, y actualiza el CSV.

Detalle que evita cientos de conexiones de más: en el respaldo normal la
identidad **se saca del `/export` ya descargado**, no de una segunda sesión SSH.
Solo el alta desde el formulario abre una conexión propia para preguntarla, y
lo hace **después** de validar el resto de los campos: al revés, una IP mal
escrita costaría un timeout completo antes de poder decir lo obvio.

Si el equipo dice llamarse como otro que ya está en el inventario, se queda con
la IP y queda constancia en el log: dos equipos con el mismo nombre compartirían
archivo en el repo y se pisarían los respaldos.

### Carga masiva por Excel

Dar de alta trescientos equipos a mano no es un plan. `/importar` descarga una
plantilla y acepta subirla llena:

- **Plantilla `.csv`** — siempre disponible, con BOM para que Excel en Windows
  no destroce las tildes.
- **Plantilla `.xlsx`** — cabecera fija, anchos por columna y una hoja de
  instrucciones aparte, para que ese texto no se lea como una fila más al
  volver a subirla.

`openpyxl` es una dependencia **opcional** (viene comentada en
`requirements.txt`; el instalador la añade si puede). Sin ella el servicio de
respaldo funciona exactamente igual y el panel simplemente ofrece solo CSV,
diciéndolo en pantalla: un respaldo desatendido de madrugada no puede quedarse
sin correr porque falte una librería que solo sirve para leer una hoja de
cálculo desde el navegador.

**Solo la columna `ip` es obligatoria.** El nombre puede ir vacío (se le
pregunta al router, ver arriba), y el puerto y el grupo tienen valor por
defecto. No hace falta respetar el orden de las columnas ni las mayúsculas, y se
aceptan los sinónimos que la gente escribe de verdad cuando rehace la plantilla
a su manera:

| Columna | También se acepta |
|---|---|
| `nombre` | `router`, `equipo`, `identidad`, `nombre del equipo` |
| `empresa` | `cliente`, `razon social` |
| `ip` | `direccion`, `direccion ip`, `host`, `ip o dns` |
| `puerto` | `puerto ssh`, `port` |
| `grupo` | `zona`, `categoria` |
| `intervalo_minutos` | `intervalo`, `cada cuanto`, `frecuencia`, `minutos` |
| `usuario` | `usuario ssh`, `user`, `login` |
| `clave` | `clave ssh`, `password`, `contrasena`, `pass` |

La comparación ignora mayúsculas, tildes, guiones bajos y espacios sobrantes:
`Dirección IP`, `DIRECCION_IP` y `direccion ip ` son la misma columna. Las
columnas que no se entienden se ignoran y se avisa de cuáles; una repetida
(`ip` y `host` a la vez) también se avisa y se usa la primera.

Las filas se validan **una a una con las mismas reglas del formulario** —
incluido el alcance: no se puede importar a una empresa que no se puede editar —
y el CSV se reescribe una sola vez al final. **Reimportar el mismo archivo no
duplica nada**: las filas repetidas se rechazan por nombre ya existente y salen
listadas con su número de fila y el motivo, para poder arreglar solo esas.

**Si llenas `usuario` y `clave`, esa hoja de cálculo pasa a ser un archivo con
contraseñas de routers**, con todo lo que eso implica (se sincroniza sola a la
nube, se manda por correo, se queda en Descargas). Bórrala del disco en cuanto
la importación haya terminado.

### Ver qué cambió y cuándo

| Ruta | Qué muestra |
|---|---|
| `/cambios` | Los últimos cambios de la flota por fecha, con equipo, empresa, commit y líneas +/-. Con filtros y selector de columnas. |
| `/historial?equipo=X` | Las versiones de un equipo, de la más reciente a la más antigua. |
| `/diferencia` | El diff de una versión concreta. Requiere el permiso `diferencias`. |

Es **solo lectura** sobre el repo: `git log` y `git show`, nunca `add` ni
`commit`. Eso es de `store.py`, que corre en el proceso del respaldo; leer no
toca el índice, así que no hay `index.lock` que disputar aunque el respaldo esté
commiteando en ese momento. Todo lo que llega por HTTP (ruta y commit) se valida
antes de pasárselo a git, y solo se aceptan hashes hexadecimales, no revisiones
simbólicas tipo `HEAD~2`.

**Y aquí va lo importante, sin adornos:** con `mostrar_secretos: true` (el valor
por defecto) el repositorio guarda **passwords PPPoE, PSK de wireless y secrets
de VPN en claro**. Publicar el diff tal cual sería repartir las credenciales de
la red a cualquiera que consiga una sesión del panel, o a cualquiera que mire la
pantalla por encima del hombro.

Por eso el panel **tapa esos valores** (`web.ocultar_secretos: true`, que es el
valor por defecto). Se conserva el nombre del atributo y se sustituye solo el
valor: se sigue leyendo "cambió la PSK de esta interfaz" sin ver cuál es. Las
líneas afectadas quedan marcadas y la página lo explica.

Consecuencia buscada, y no un defecto: **si lo único que cambió en una línea fue
el propio secreto, las dos versiones se ven idénticas**. Se ve que la línea
cambió — el par `-`/`+` sigue ahí — sin revelar ni el valor viejo ni el nuevo.
Que es justo el momento en que una clave se filtra: cuando se rota.

La lista de atributos que se tapan es explícita (`ATRIBUTOS_SECRETOS` en
`mkbackup/historial.py`), no una heurística: una heurística llenaría el diff de
marcas hasta hacerlo inútil, y dejar de tapar uno solo reparte esa credencial.
No se tapan los interruptores (`require-password=yes` no dice cuál es la clave,
dice que hay que pedirla) ni los valores vacíos.

| Opción | Por defecto | Qué hace |
|---|---|---|
| `ver_diferencias` | `true` | En `false` desaparecen los enlaces al diff y `/diferencia` responde 403. |
| `ocultar_secretos` | `true` | Enmascara passwords, PSK y secrets en el diff y en el contenido de una versión. |
| `historial_maximo` | `50` | Cuántas versiones se listan por equipo y cuántos cambios en `/cambios`. |

Ponerlo en `false` es una decisión consciente, no un descuido, y el panel
**avisa en el log al arrancar** de que va a mostrar las credenciales a
cualquiera que entre.

### Ajustes desde el panel

`/ajustes` (permiso `ajustes`) cambia tres cosas **sin reiniciar nada y sin
reescribir ninguna unidad de systemd**:

1. **Cada cuánto se respalda.** El intervalo **general** del programador
   (`intervalo_minutos`, el que heredan los equipos que no traen el suyo) y si
   se respalda nada más arrancar (`al_arrancar`). El programador relee los
   ajustes mientras espera, así que bajar de 4 horas a 30 minutos surte efecto
   en el momento y no dentro de 4 horas, que es justo cuando alguien está
   esperando a ver si funcionó.
2. **Las credenciales SSH generales.** Usuario y clave con los que se entra a
   cualquier equipo que no traiga las suyas — lo que hay que cambiar cuando se
   rota la clave de la flota. El usuario no puede quedar vacío; la clave vacía
   significa "no la toques", porque guardar la cadena vacía dejaría a la flota
   entera sin credencial. La clave **no se manda al navegador ni para rellenar
   el campo**, pero sí se dice si hay alguna puesta: un campo vacío sin más no
   distingue "no la toques" de "no hay ninguna".
3. **El fondo de la pantalla de entrada** (ver arriba).

Lo que se cambia en el panel **no se escribe en el YAML**: va a
`almacen.ajustes` (`/root/mkbackup/ajustes.json` por defecto) y **manda sobre
`config.yaml`**. Si cambias algo en el YAML y no ves el efecto, es que hay un
ajuste guardado desde el panel.

**Por qué un archivo aparte y no `config.yaml`:** las dos unidades corren con
`ProtectSystem=strict`, y `config.yaml` es un archivo que **edita una persona
con root**. Está lleno de comentarios que un volcado automático se comería, y
lleva las rutas del repositorio, el remoto de replicación y los parámetros del
propio panel. Darle permiso de escritura sobre él al proceso que atiende
peticiones HTTP sería darle la capacidad de apuntar los respaldos a otro sitio.
Lo que el panel puede tocar es una **lista blanca de cinco claves**
(`AJUSTES_EDITABLES` en `config.py`), y todo lo demás sigue siendo de archivo.

Las credenciales SSH **sí** están en esa lista, y el precio hay que decirlo
claro: **quien entre al panel como administrador puede cambiar con qué
credenciales se entra a los routers.** Se aceptó porque pedir un root y un
editor para rotar la clave de la flota llevaba a que no se rotara nunca. El
archivo de ajustes se escribe con `0600` por eso mismo, y el cambio queda en la
auditoría — el usuario sí, la clave nunca.

Tres detalles del programador que se notan con flotas grandes:

- **Un ciclo solo toca a quien le toca.** Con intervalos por equipo, la espera
  se calcula hacia el **primer vencimiento de la flota**, no hacia el intervalo
  general: con un equipo a 30 minutos y un general de 4 horas, dormir 4 horas
  dejaría a ese equipo sin respaldar a tiempo. Cuándo se consultó cada equipo
  por última vez se guarda en `programador.json`, porque un equipo sin cambios
  no deja commit del que deducir la fecha y sin esa memoria cada reinicio del
  servicio volvería a respaldar los 300 de golpe.
- **El intervalo descuenta lo que tardó el ciclo**: con 240 minutos y un ciclo
  de 6, la siguiente espera son 234, no 240. Si no, cada vuelta se retrasaría un
  poco más y el respaldo iría corriéndose de hora.
- **Si un ciclo dura más que el intervalo, no se encadena otro encima.** git no
  admite dos escrituras sobre el mismo índice, así que se degrada a "lo más
  seguido que se pueda" con una pausa mínima entre ciclos, y queda avisado en el
  log de que el intervalo configurado no da para la flota que hay.

### Las fechas van en hora de Ecuador

Las marcas de tiempo se **guardan siempre en UTC** —una hora local dentro de un
archivo es una bomba de relojería el día que el servidor cambie de zona— y solo
se convierten **al mostrarlas**. La zona se configura con `zona_horaria` en
`config.yaml`, por defecto `America/Guayaquil`.

Si la base IANA no está disponible (Windows sin `tzdata`), para
`America/Guayaquil` se usa un desfase fijo de −5, que es exacto todo el año
porque Ecuador no tiene horario de verano. Para cualquier otra zona eso sería
mentir, así que ahí se avisa en el log y las fechas se quedan en UTC.

### Cómo sabe distinguir "corriendo" de "se murió a medias"

Mientras la ejecución vive, refresca un **latido** cada 5 segundos en
`estado.json`. Si el archivo dice que no terminó pero el latido lleva más de
40 segundos parado, el proceso se cortó — `kill`, OOM, corte de luz — y el
panel lo reporta como **INTERRUMPIDA**.

Sin eso, una ejecución muerta se quedaría "en curso" para siempre, que es la
forma más rápida de que el panel deje de creerse justo cuando hay que mirarlo.

### Lo que el panel no hace, a propósito

- **No lanza respaldos.** Un botón "respaldar ahora" convierte una página de
  consulta en algo que toca 300 equipos de golpe. El disparo es del programador;
  desde aquí solo se cambia cada cuánto.
- **No restaura equipos.** Ni el `.rsc` ni el `.backup`. Eso es a mano, con la
  cabeza puesta y mirando la versión de RouterOS.
- **No edita las rutas ni la configuración general.** Solo la lista blanca de
  `AJUSTES_EDITABLES`. Las rutas del repositorio, el remoto de replicación y
  Telegram viven en `config.yaml`, que se toca con un editor y con root. El
  panel escribe únicamente bajo `/root/mkbackup`: el inventario, los ajustes,
  las cuentas, la auditoría y la imagen de fondo del login.
- **No enseña los secretos de las configuraciones.** El diff los tapa (ver
  arriba). Se ve qué cambió, no lo que dice.
- **No tiene HTTPS propio, ni API REST, ni 2FA, ni recuperación de clave por
  correo.** Hay un `/api/estado` con el mismo JSON que pinta la pantalla de
  estado, y nada más. Una clave olvidada se arregla desde el panel por otro
  administrador, o con `--clave-usuario`.

### Antes de exponerlo

Ya pide login, pero **habla HTTP en claro**. En `127.0.0.1` da igual, y ahí es
donde escucha por defecto. Si cambias `direccion` a `0.0.0.0`, la clave del panel
y la cookie de sesión viajan legibles por la LAN, y entonces el login no protege
gran cosa. **No hay HTTPS propio**: para verlo desde otra máquina, pon delante
un proxy con TLS (nginx, Caddy) apuntando a `127.0.0.1:8080`. El
propio panel avisa en el log si lo arrancas escuchando fuera de localhost.

La cookie va sin `Secure` a propósito, porque sobre HTTP el navegador la
descartaría; si montas el proxy TLS, ese es el sitio donde añadirlo.

Las respuestas llevan `Content-Security-Policy: default-src 'self'
'unsafe-inline'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` y
`Referrer-Policy: same-origin`. El panel no carga nada de fuera: ni fuentes, ni
CDN, ni librerías de gráficas.

`/salud` responde `ok` **sin sesión, y es deliberado**: es la ruta que mira un
monitor externo para saber que el proceso vive, y pedirle credenciales obligaría
a repartir la clave del panel por cada sistema de monitoreo. No revela nada de la
flota. `/fondo` también es abierta, por necesidad (ver arriba). Todo lo demás
exige sesión: `/` redirige al formulario y `/api/estado` —el JSON crudo, si
prefieres consumirlo desde otro sitio— responde **401** para que un script sepa
que le falta login en vez de tragarse el HTML del login como si fuera el estado.

---

## Replicación off-site

Si `mostrar_secretos: true` (por defecto), el repo contiene passwords PPPoE, PSK
de wireless y secrets de VPN **en texto plano**. Replicarlo a GitHub, aunque sea
privado, pone las credenciales de toda tu red en un tercero.

Para replicar con seguridad, usa un remoto cifrado:

```bash
apt install git-remote-gcrypt
gpg --batch --gen-key                    # clave dedicada, como root
# en config.yaml:
#   remoto: "gcrypt::git@github.com:usuario/repo-privado.git"
```

`git-remote-gcrypt` cifra los objetos antes de subirlos: el remoto ve blobs
opacos. **Guarda la clave GPG fuera del servidor** — sin ella el mirror es
ilegible, y un mirror que no se puede descifrar no es un respaldo.

---

## Diferencias con Oxidized

`mkbackup` nació para cubrir lo que Oxidized no hace: el **backup binario**.

En lo demás se parecen más de lo que se parecían: aquí también se navegan las
versiones de cada equipo y se leen los diffs desde el navegador, y también hay
cuentas y roles.

Lo que Oxidized tiene y esto no:

- **Decenas de fabricantes.** Aquí solo hay RouterOS, y todo — el parseo, la
  limpieza del ruido, la identidad del equipo — está escrito para él.
- **API REST y hooks.** Aquí solo `/api/estado` y Telegram.
- **Años de casos raros ya resueltos**, y no lo mantienes tú.

Lo que hay aquí y no allí, además del binario:

- El **enmascarado de secretos** en el diff. Con `show-sensitive` activado,
  publicar configuraciones por HTTP es publicar las credenciales de la red;
  aquí se tapan los valores y se sigue viendo qué línea cambió.
- El **alcance por empresa**, pensado para un ISP que administra los equipos de
  varios clientes en el mismo panel, con el 404 en vez del 403 y con los totales
  y las gráficas recortados.

Para buscar dentro de las configuraciones sigue estando `git grep`, que con 300
equipos es más rápido que cualquier buscador que le pongas encima.

Si solo necesitas el `/export` en texto, Oxidized es más maduro y no tienes que
mantenerlo tú.

---

## Estructura

```
mkbackup/
├── config.py       Carga y validación de config.yaml, y los ajustes del panel
├── inventory.py    Lectura y escritura del CSV, tolerante y con avisos
├── importar.py     Alta masiva desde una plantilla CSV o Excel
├── device.py       SSH a RouterOS: export, backup binario, normalización
├── store.py        git para el texto, disco con retención para el binario
├── historial.py    Lectura del historial de git y enmascarado de secretos
├── estado.py       Avance de la ejecución en JSON, con latido
├── hechos.py       Modelo, versión y último respaldo bueno de cada equipo
├── sesion.py       Hash de la clave y sesiones en memoria
├── usuarios.py     Cuentas, roles, permisos y alcance por empresa
├── auditoria.py    Quién entró, quién lo intentó y quién tocó qué
├── web.py          Rutas del panel: lo que se decide en cada petición
├── paginas.py      HTML del panel: lo que se ve en cada petición
├── planificador.py Bucle residente: a qué equipos les toca y cuándo
├── notify.py       Telegram
└── cli.py          Orquestación: paralelo para SSH, serie para git
```

No hay más: ni carpeta de plantillas, ni de estáticos, ni framework. El panel es
`http.server` de la librería estándar, el HTML se genera en `paginas.py` y las
únicas dependencias son `paramiko` y `PyYAML` (más `openpyxl`, opcional, para el
`.xlsx`).

Los archivos de datos, todos bajo `/root/mkbackup`:

| Archivo | Qué guarda | Quién lo escribe |
|---|---|---|
| `configs.git/` | Los `.rsc` versionados | el respaldo |
| `binarios/` | Los `.backup`, con retención | el respaldo |
| `inventory.csv` | La flota | los dos |
| `estado.json` | El ciclo en curso, con latido | el respaldo |
| `programador.json` | Cuándo se consultó cada equipo | el programador |
| `equipos.json` | Modelo, versión y último respaldo bueno | el respaldo |
| `ajustes.json` | Lo que se cambia desde el panel | el panel |
| `usuarios.json` | Cuentas, roles y hashes | el panel |
| `auditoria.log` | Un evento por línea | el panel |

Detalle de concurrencia: los equipos se consultan **en paralelo** (lo lento es
la red) pero se guardan **en serie**, porque git no admite dos escrituras
simultáneas sobre el mismo índice (`index.lock`). Esa separación es lo que
permite subir la concurrencia sin corromper el repositorio.

## Pruebas

```bash
python -m tests.test_limpieza     # normalizacion del export
python -m tests.test_almacen      # git, deteccion de cambios, retencion
python -m tests.test_estado       # avance, latido, concurrencia, escritura atomica
python -m tests.test_sesion       # hash de la clave, caducidad, bloqueo por IP
python -m tests.test_historial    # log, diffs y enmascarado de secretos
python -m tests.test_importar     # cabeceras, sinonimos, csv y xlsx
python -m tests.test_planificador # intervalos, descuento del ciclo, pausa minima
python -m tests.test_usuarios     # roles, permisos, alcance y la invariante
python -m tests.test_hechos       # modelo, version, ultimo ok y dato vacio
```

**857 comprobaciones** entre los nueve archivos, todas en verde hoy. No
requieren ningún equipo ni red.

| Módulo | Comprobaciones |
|---|---|
| `test_limpieza` | 73 |
| `test_almacen` | 41 |
| `test_estado` | 35 |
| `test_sesion` | 76 |
| `test_historial` | 161 |
| `test_importar` | 95 |
| `test_planificador` | 79 |
| `test_usuarios` | 259 |
| `test_hechos` | 38 |

### Probar el flujo completo sin hardware

`tests/simulador_routeros.py` levanta un servidor SSH que responde como un
RouterOS 7, con el ruido que cambia en cada llamada. Sirve para comprobar que
el respaldo funciona de punta a punta antes de tocar un equipo real.

```bash
# Terminal 1
python -m tests.simulador_routeros 2222

# Terminal 2 — inventory.csv apuntando a 127.0.0.1:2222,
# config.yaml con usuario Respaldo / password prueba123
python -m mkbackup.cli --sin-binario          # 1a vez: crea commit
python -m mkbackup.cli --sin-binario          # 2a vez: "0 con cambios"
echo "1.1.1.1" > tests/.dns_simulado          # cambio real
python -m mkbackup.cli --sin-binario          # detecta el cambio

git -C datos/configs.git log --oneline        # deben ser 2 commits, no 3
```

Ese `.dns_simulado` cambia la configuración que sirve el simulador en caliente,
para provocar un cambio real sin reiniciarlo.

**El simulador no implementa SFTP**, así que hay que usar `--sin-binario`: la
descarga del `.backup` solo se puede probar contra un equipo real.

Detalle: el simulador **no** usa `SO_REUSEADDR` a propósito. En Windows eso
permite que dos procesos bindeen el mismo puerto, y un simulador viejo quedándose
con el tráfico hace que las pruebas "pasen" contra la versión equivocada del
código. Si el puerto está ocupado, falla y lo dice.
