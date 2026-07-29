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
  autenticación, `/export`, limpieza, commit. Cuatro ejecuciones seguidas dejan
  **dos** commits de respaldo —el alta y un cambio real de DNS—, porque una
  ejecución que no encuentra ningún cambio no genera commit. (El repositorio
  arranca con su propio commit de creación, así que `git log` enseña tres
  líneas en total.)
- **Alta con nombre automático**: un equipo dado de alta sin nombre responde su
  `/system identity` y entra con él; el que no responde entra con su IP y el
  primer respaldo correcto lo renombra solo, moviendo su histórico con `git mv`
- **Cuentas, roles, permisos y alcance por empresa** (`tests/test_usuarios.py`),
  incluida la invariante del último administrador total y las migraciones del
  archivo de usuarios, que va ya por el **formato 4** (ver
  [Cuentas, roles y permisos](#cuentas-roles-y-permisos))
- **Llevarse el servidor a otra máquina** (`tests/test_mudanza.py`): empaquetar,
  restaurar sobre datos de verdad y que el destino quede igual que el origen
- **Lo que se sabe de cada equipo** —modelo, versión, último respaldo bueno— y
  la regla de que un dato vacío nunca pisa uno bueno (`tests/test_hechos.py`)
- **Importación** de plantillas CSV y `.xlsx`, **diffs enmascarados** y el
  **panel** entero (login, altas, bajas, edición, historial, usuarios,
  auditoría, ajustes)
- **Intervalo por equipo**: a quién le toca, herencia del intervalo general,
  descuento del ciclo y suelo del tic (`tests/test_planificador.py`)
- Manejo de equipo inalcanzable: respeta el timeout, reintenta (`reintentos`,
  `espera_reintento`) y sale con código 1 sin colgarse

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
regenerado únicamente cuando el texto delató un cambio de verdad
(`backup_binario.solo_si_cambio`). Si no quieres binarios en absoluto,
`backup_binario.activo: false`.

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
commitea si el resultado difiere del anterior. El export se pide además en modo
`terse` (`export.terse`, por defecto `true`): una línea por comando, que es lo
que hace legibles los diffs.

Las pruebas de `tests/test_limpieza.py` cubren esto en ambas direcciones: que el
ruido no cuente como cambio, y que un cambio real sí se detecte.

---

## Instalación, en detalle

### Todo vive bajo /root, y lo que eso cuesta

Los datos —repositorio git (`configs.git/`), binarios, inventario,
`estado.json`, `ajustes.json`, `usuarios.json`, `equipos.json`,
`programador.json`, `replica.json`, `auditoria.log` y la imagen de fondo del
login— van a **`/root/mkbackup`** (la lista completa, con quién escribe cada
uno, está en [Estructura](#estructura)), y las dos unidades de systemd corren
con `User=root`,
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
en vez de al puerto SSH. La fila que deja `puerto` vacío hereda
`ssh.puerto_defecto` (22).

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
scp BTS-Norte-01_20260725_120000_123456.backup admin@10.20.30.11:
ssh admin@10.20.30.11 "/system backup load name=BTS-Norte-01_20260725_120000_123456"
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

Dónde escucha sale de `web.direccion` y `web.puerto`; la pantalla de Estado se
repinta sola cada `web.refresco` segundos (3 por defecto).

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
| `sesion_minutos` | `30` | Minutos **sin actividad** antes de cerrar la sesión. Se reinicia al usar el panel. |
| `sesion_horas` | `8` | **Tope absoluto** de la sesión, se esté usando o no. |
| `intentos_max` | `5` | Fallos seguidos desde una IP antes de frenarla. |
| `bloqueo_segundos` | `300` | Lo que espera esa IP tras pasarse de intentos. |
| `eventos_por_pagina` | `30` | Eventos por página en la auditoría. |
| `fondo_login` | *(vacío)* | Imagen de la pantalla de entrada. Se sube desde Ajustes. |

El bloqueo es **por IP** y en memoria: cinco intentos fallidos y esa dirección
queda cinco minutos fuera, con la respuesta 429 diciendo cuánto le falta. Un
acierto borra su contador, y desde `/auditoria` se puede levantar a mano.

**La sesión tiene dos relojes y cierra el que llegue primero.**
`sesion_minutos` (30) es el de **inactividad**: se reinicia cada vez que se usa
el panel de verdad, y el refresco automático de la pantalla de Estado **no
cuenta** —si contara, una pestaña olvidada en un ordenador encendido mantendría
la sesión viva para siempre, que es justo la situación contra la que existe—.
`sesion_horas` (8) es el **tope absoluto**: aunque se esté trabajando sin parar,
pasadas esas horas hay que volver a escribir la clave, y eso acota lo que sirve
una sesión robada. `sesion_minutos` no puede pasar de `sesion_horas`: el panel
se niega a arrancar en vez de aplicar un tope que nunca se alcanzaría.

La sesión es una cookie `HttpOnly` y `SameSite=Strict`, **sin `Max-Age`**: es
una cookie de sesión del navegador, así que cerrar el navegador la borra en ese
mismo momento. Con `Max-Age` la cookie sobrevivía al navegador cerrado, que en
un puesto compartido es exactamente lo que no se quiere. El token es opaco y
vive en memoria del proceso, no firmado. El
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

**El archivo va por el formato 4**, y las migraciones son automáticas al leerlo
(`FORMATO` y `_migrar` en `usuarios.py`). La regla de todas ellas es la misma:
*a nadie se le cambian los permisos por actualizar*.

| Formato | Qué trajo | Qué hace la migración |
|---|---|---|
| 1 | Tres roles fijos en el código, sin alcance ni excepciones. | — |
| 2 | Los roles pasan a ser **datos**, más `permisos_mas` / `permisos_menos` y alcance por usuario. | Se escriben los tres roles de siempre y cada cuenta conserva el suyo con `alcance.todo` — en el formato 1 no había alcance, o sea que todo el mundo veía todos los equipos. |
| 3 | **Permisos finos**: `ver`, `ajustes` y `usuarios` se parten en uno por pantalla y por acción. | Se traducen los tres permisos gruesos a los finos equivalentes (`EQUIVALENCIAS`), en los roles y en las excepciones de cada cuenta. Es obligatorio, no cosmético: un permiso desconocido se ignora, así que sin traducir, `usuarios` dejaría de contar y el panel se quedaría **sin ningún administrador**. |
| 4 | Aparece `datos.mudanza`: descargar una copia entera del servidor. | Se le da **solo al rol que ya tenía los 24 permisos anteriores** (`PERMISOS_FORMATO_3`), y a ninguno más. |

Que la exclusión también se traduzca importa tanto como la concesión: quien
tenía **quitado** `ajustes` sigue sin poder entrar a Ajustes, y eso ahora son
ocho permisos. Traducir solo los `mas` le devolvería en silencio un acceso que
alguien le retiró a propósito.

Hay **veinticinco permisos** (`AREAS`, `PERMISOS` y `ETIQUETAS_PERMISO` en
`mkbackup/usuarios.py`). Es una lista cerrada a propósito: un permiso que solo
existiera en el archivo de un cliente no lo comprobaría nadie. Van agrupados
por área porque así se pintan en la pantalla de roles —treinta casillas
seguidas no se entienden— y el orden es de menos a más peligroso: primero
mirar, luego tocar la flota, luego la configuración y al final las cuentas.

| Área | Permiso | Qué abre |
|---|---|---|
| **Consultar** | `estado.ver` | Ver la pantalla de Estado |
| | `equipos.ver` | Ver la lista de equipos |
| | `cambios.ver` | Ver la lista de cambios |
| | `diferencias` | Ver el contenido de los cambios (diffs) |
| **La flota** | `equipos.crear` | Añadir equipos |
| | `equipos.editar` | Editar equipos |
| | `equipos.baja` | Dar de baja equipos |
| | `equipos.importar` | Importar desde Excel |
| | `equipos.identidades` | Preguntarle el nombre a los routers |
| **Ajustes** | `ajustes.ver` | Abrir la pantalla de Ajustes |
| | `ajustes.programador` | Cambiar cada cuánto se respalda |
| | `ajustes.respaldar` | Lanzar un respaldo ahora |
| | `ajustes.ssh` | Cambiar el acceso SSH a los routers |
| | `ajustes.remoto` | Cambiar a dónde se suben los respaldos |
| | `ajustes.fondo` | Cambiar la imagen de la pantalla de entrada |
| | `datos.borrar` | BORRAR los respaldos de un equipo |
| | `datos.mudanza` | DESCARGAR una copia entera del servidor |
| **Cuentas y registro** | `usuarios.ver` | Ver las cuentas y los roles |
| | `usuarios.crear` | Añadir cuentas |
| | `usuarios.editar` | Editar cuentas (rol, permisos y alcance) |
| | `usuarios.baja` | Borrar cuentas |
| | `roles.editar` | Crear y editar roles |
| | `roles.baja` | Borrar roles |
| | `auditoria.ver` | Ver el registro de auditoría |
| | `auditoria.desbloquear` | Desbloquear direcciones IP |

**Un permiso arrastra el de ver su pantalla** (`HERENCIA`): quien puede editar
equipos ve la lista de equipos, quien puede tocar cualquier cosa de Ajustes
tiene `ajustes.ver`, quien desbloquea IPs ve la auditoría. Se aplica al
calcular los efectivos y no al guardar, para que la casilla que alguien marcó
siga siendo la que ve marcada. Sin eso quedan roles que "pueden editar equipos"
y reciben un 403 en la pantalla que esa misma persona acaba de habilitar, y eso
no se lee como una configuración estricta sino como un panel roto.

#### Por qué `datos.mudanza` va aparte

`datos.mudanza` se pinta en Ajustes, pero **no se reparte con el resto del
área**, y es deliberado: quien cambia cada cuánto se respalda no tiene por qué
poder descargarse las credenciales de la flota entera. Por eso tampoco se
tradujo con `EQUIVALENCIAS` como los permisos gruesos del formato 2 — aquello
traducía permisos que la gente **ya** tenía; esto es uno nuevo que no tenía
nadie, y un permiso que descarga las claves de todos los clientes no se regala a
quien resulte que encaja en una equivalencia. Al **quitar** sí va incluido
(`EQUIVALENCIAS_AL_QUITAR`): excluir a alguien de Ajustes tiene que excluirlo
del área entera, incluido lo que se añadió después.

Además **exige alcance total**, no solo el permiso. La copia es del servidor
entero —no existe la copia de un solo cliente—, así que sin esa segunda
comprobación una cuenta limitada a una empresa se llevaría de una vez justo lo
que el alcance le niega equipo a equipo. Ver
[Mudarse a otro servidor](#mudarse-a-otro-servidor).

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

> **Siempre tiene que quedar al menos un usuario que tenga a la vez los
> permisos `usuarios.ver` y `usuarios.editar` (`LLAVES`) y alcance a todo.**

Son los dos y hacen falta los dos: `usuarios.editar` es lo que permite
devolverle el rol o el permiso a alguien, y `usuarios.ver` es lo que permite
llegar a esa pantalla. Con uno solo queda una cuenta que teóricamente manda
pero no encuentra la puerta, que a efectos prácticos es un panel cerrado.

Ese es el único que puede volver a repartirlo todo: crear cuentas, arreglar
roles y dar acceso a cualquier empresa. Sin ninguno, el panel se queda sin
llaves y la única salida es editar el JSON a mano por SSH.

Se protege en **todas** las operaciones que podrían romperla, incluidas las
indirectas: borrar esa cuenta, cambiarle el rol, quitarle el permiso con
`permisos_menos`, recortarle el alcance, **quitarle esos permisos al rol
que se los daba**, o borrar ese rol. No se mira "qué campo cambió": se calcula
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

La lista entera de eventos es `EVENTOS` en `mkbackup/auditoria.py`:

| Grupo | Evento | Qué fue |
|---|---|---|
| **Accesos** | `login_ok` | Entró al panel |
| | `login_fallido` | Intento fallido |
| | `login_bloqueado` | Bloqueado por intentos |
| | `desbloqueo` | Desbloqueó una dirección |
| | `salir` | Cerró sesión |
| | `sin_pantallas` | Entró sin permiso para ver nada |
| **Rechazos** | `sin_permiso` | Intentó algo sin permiso |
| | `fuera_de_alcance` | Intentó ver algo fuera de su alcance |
| **Cuentas** | `clave_propia` | Cambió su propia clave |
| | `usuario_alta` | Creó una cuenta |
| | `usuario_cambio` | Modificó una cuenta |
| | `usuario_baja` | Borró una cuenta |
| | `rol_cambio` | Modificó un rol |
| | `rol_baja` | Borró un rol |
| **La flota** | `equipo_alta` | Dio de alta un equipo |
| | `equipo_cambio` | Editó un equipo |
| | `equipo_baja` | Dio de baja un equipo |
| | `importacion` | Importó equipos |
| | `identidades` | Le preguntó el nombre a los routers |
| | `datos_borrados` | Borró los respaldos de un equipo |
| **Configuración** | `ajustes` | Cambió los ajustes |
| | `acceso_ssh` | Cambió el acceso a los routers |
| | `respaldo_manual` | Pidió un respaldo inmediato |
| | `remoto` | Cambió a dónde se suben los respaldos |
| | `remoto_prueba` | Probó la conexión con el repositorio |
| **El servidor entero** | `mudanza` | Descargó una copia de todo el servidor |
| | `mudanza_restaurada` | **RESTAURÓ** una copia sobre este servidor |

`mudanza` es el evento más importante del registro: ese archivo lleva dentro el
inventario con las claves de los routers, las cuentas del panel y el histórico
completo. Quien lo tiene, tiene la red. Y `mudanza_restaurada` es el único que
casi siempre **se pierde** —el registro es una de las piezas que se
reemplazan—, así que va también al log del servicio, que no lo toca nadie.

`sin_pantallas` tiene etiqueta propia porque no es un intento de colarse: es
alguien a quien se le creó la cuenta y se olvidó darle acceso, y eso solo se
distingue en el registro si se llama distinto. Los cuatro que dibujan un intento
de entrar sin permiso —`login_fallido`, `login_bloqueado`, `sin_permiso` y
`fuera_de_alcance`— son los `SOSPECHOSOS`, el filtro de la pantalla.

**Nunca guarda claves**, ni hashes, ni tokens de sesión. De un cambio de
credenciales SSH se anota que cambió y quién, no el valor. Sí se anota el nombre
**tecleado** en un login fallido aunque no exista ninguna cuenta así: media
auditoría de un ataque es saber qué cuentas estuvieron probando. Cada valor se
recorta a una línea sin caracteres de control — si no, quien teclee un salto de
línea en el formulario puede inventarse líneas enteras del registro.

El archivo **se rota solo** al llegar a 5 MB (del orden de 30.000 eventos) y
conserva una generación anterior en `.log.1`. Quien necesite historia larga que
se lo lleve a donde guarde los logs de verdad.

La pantalla `/auditoria` —permiso `auditoria.ver`— filtra por tipo
de evento, por usuario y por "solo sospechosos", con paginación de
`web.eventos_por_pagina` eventos (30 por defecto). Si se pide una página que no
existe se enseña la última completa, y el título dice el rango real en vez de
mentir. Los intentos fallidos y los bloqueos van en rojo.

Arriba hay una tabla de **direcciones con intentos fallidos**, con cuántos lleva
cada una y cuánto le queda de bloqueo, y un botón **Desbloquear** —permiso
aparte, `auditoria.desbloquear`, que arrastra `auditoria.ver`— para el caso
del despiste: cinco errores de teclado no tienen por qué costar cinco minutos.
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
equipo por equipo, los avisos del inventario y las **cinco últimas ejecuciones**.

Fueron veinte, luego diez y ahora cinco (`HISTORIAL_MAXIMO` en `estado.py`).
Diez seguían siendo diez filas casi idénticas —"1/1, 0 cambios, 18 s"— una
debajo de otra, y una tabla en la que todas las filas dicen lo mismo no se lee:
se salta entera. Para la tendencia a largo plazo están los commits de git, que
no caducan.

Arriba hay una fila de totales — **Empresas, Equipos, Respaldados, Fallidos,
Próximo ciclo** — y debajo dos gráficas de tarta con leyenda y conteos:

| Gráfica | Qué reparte |
|---|---|
| **Resultado del último respaldo** | Sin cambios / Con cambios / Fallidos / Sin respaldar. |
| **Equipos por cliente** | Los cuatro clientes con más equipos, y el resto plegado en "Otros". |

Los recuadros dicen **Empresas** y no "Clientes" porque *empresa* es como se
llama ese campo en el formulario de alta, en la columna de la tabla y en el
filtro. Con dos palabras para la misma cosa hay que adivinar qué cuenta cada
recuadro, y "no sé qué está contando" fue exactamente lo que preguntó quien lo
usa. Detrás de los cinco de siempre aparecen, **solo si hay algo que contar**,
**Empresas con fallos**, **Empresas al día** y **Empresas pendientes**: un "0
empresas con fallos" permanente enseña a no mirar ese sitio, y entonces el día
que ponga 2 tampoco se mira.

Los totales se cuentan sobre los **equipos** y no sobre los contadores del
ciclo: con intervalos por equipo, un ciclo puede tocar solo a una parte de la
flota, y "3 de 3" no dice nada de los otros 297. Y todo esto se recorta al
alcance de quien mira (ver [Multitenant](#multitenant-quién-ve-qué-equipos)).

#### La flota que se cuenta es la de ahora, no la del último ciclo

La lista la manda el **inventario**, y encima se le pega lo que se sepa del
último ciclo — no al revés (`_equipos_del_estado` en `web.py`). Antes se pintaba
la foto que dejó el ciclo, y eso se veía como una cifra que no cuadra con la
realidad, en las dos direcciones:

- Dabas de baja un equipo y Estado **seguía contándolo durante horas**, hasta el
  siguiente ciclo. Con un solo router en la flota, el panel decía "1 equipo, 1
  respaldado" sobre uno que ya no existía.
- Dabas de alta otro y **no aparecía hasta que le tocara turno**, así que Estado
  decía "3 equipos" mientras en Equipos había 4.

Ahora un equipo recién dado de alta sale como **pendiente**, que es exactamente
lo que es, y uno dado de baja desaparece en el acto. La empresa y el grupo
también se toman del inventario y no de la foto: si a un equipo le cambiaron de
cliente esta mañana, la tarta tiene que pintarlo donde está ahora.

Si el inventario no se puede leer, se cae a lo que diga el estado. Es dato
viejo, pero es mejor que una pantalla en blanco cuando lo que falla es el CSV.

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

### Borrar los datos de un equipo

Dar de baja no borra los respaldos, y ese es el comportamiento que se quiere
casi siempre. Pero un cliente que se va puede pedir que no quede nada suyo, y
"está borrado pero sigue en el historial" no es eso. La tarjeta **Borrar los
datos de un equipo**, en `/ajustes`, es la única forma de sacar un respaldo del
repositorio desde el panel.

**Solo salen los equipos dados de baja**: los que tienen archivos en el
repositorio pero ya no están en el inventario. Un equipo activo no aparece en la
lista, así que no hay forma de borrar por descuido lo de un router que se sigue
respaldando. La comparación es contra la **ruta** (`empresa/equipo.rsc`) y no
contra el nombre: dos clientes pueden tener un "Router-Principal", y mirar solo
el nombre daría por vivo el archivo del que se fue.

Se ofrecen **las dos formas**, y elige quien borra:

| Forma | Qué hace | Se deshace |
|---|---|---|
| **Quitarlo del repositorio** | `git rm` y commit. El archivo desaparece de la versión de hoy. | Sí: sus versiones anteriores siguen en git (`git checkout <commit>^ -- <ruta>`). |
| **Borrarlo también del historial** | Reescribe el repositorio (`git filter-branch --index-filter`) para que no quede en ninguna versión, y recoge lo que sobra (`refs/original`, reflog, `gc --prune=now`). | **No.** |

En los dos casos se borran también los `.backup` de ese equipo: son
certificados y claves SSH de alguien que ya no está.

Reescribir la historia **cambia el identificador de todos los commits**, así que
un repositorio ya subido a un remoto queda divergido y el siguiente push se
rechaza. Está dicho en pantalla, y es el precio de que el dato desaparezca de
verdad.

Tres frenos, porque esto no tiene marcha atrás:

- **Hay que escribir el nombre exacto del equipo** en un campo. Sin eso no se
  borra nada. Es lo único que separa este botón de un clic en la fila de al
  lado, así que se compara tal cual, sin recortes ni mayúsculas.
- **La ruta se vuelve a buscar en la lista** antes de tocar nada: que el
  formulario la mande no significa nada, un formulario se edita.
- **Con un ciclo en marcha se niega** (409). El programador es otro proceso
  escribiendo en ese mismo repositorio, y purgar lo reescribe entero.

Hace falta el permiso `datos.borrar`, que es suyo y no `equipos.baja` porque son
cosas distintas: dar de baja deja los respaldos donde están, y esto los saca del
repositorio. Queda en la
auditoría como `datos_borrados` con la ruta y la forma. En **multitenant** solo
lo ve quien lo ve todo: un equipo dado de baja ya no está en el inventario y no
se le puede calcular el alcance, la misma regla que en `/cambios`. Adivinar la
empresa por el nombre de su carpeta dejaría que quien acierte el slug se llevara
por delante los respaldos del cliente de al lado.

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

### Preguntarle el nombre a toda la flota

En **Ajustes** hay el mismo botón, pero para muchos equipos de golpe. Dos
alcances: **solo los que se llaman como su IP** (lo normal después de una
importación por Excel) o **todos**, que hace mandar al router por encima del
nombre escrito a mano.

Esto **no cabe en una petición HTTP** y esa es la razón de que exista
`identidades.py` en vez de un puñado de líneas en `web.py`. Preguntarle a uno
son dos segundos; a trescientos, con los apagados agotando su timeout, son
minutos. El navegador se rinde antes y el manejador del panel corta a los 30
segundos. Así que el botón solo **arranca** el trabajo: corre en un hilo aparte
con el mismo paralelismo que los respaldos (`concurrencia`), y la pantalla lo
mira por `/api/identidades` sin esperarlo. Se puede cerrar el navegador.

Las tres reglas que no se pueden romper, y por qué:

- **No se renombra a ciegas.** Cada nombre que llega de un router pasa por la
  misma validación que un alta a mano. El choque más común es real: dos routers
  de clientes distintos llamados los dos `MikroTik`.
- **No se pierde el histórico.** Primero se mueve el archivo con `git mv` y solo
  si eso sale bien se toca el CSV. Al revés, el inventario apuntaría a un
  respaldo que no está.
- **No se pierden las credenciales del equipo.** Renombrar cambia *un* campo.
  Esto no era así: el renombrado automático llamaba a `validar_equipo` sin
  pasarle `usuario`, `clave` ni `intervalo_minutos`, así que un router con clave
  propia se quedaba sin ella el día que se renombraba solo, y **fallaba
  autenticando en el ciclo siguiente** — un ciclo más tarde y en otro sitio, que
  es la forma más cara de enterarse.

Mientras el programador está en un ciclo, el botón **se niega**. No es prudencia:
ese ciclo también renombra equipos, y son dos procesos distintos escribiendo el
mismo CSV y el mismo repositorio de git. El candado del panel no cruza esa
frontera.

Por alcance, cada cuenta pregunta solo a los equipos que **puede editar**, y el
avance solo lo ve quien lo lanzó o quien alcanza a la flota entera: el sondeo es
uno para todo el panel, y lo que lleva dentro son nombres de equipos.

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

**"Todas las versiones" de un equipo dado de baja ya no responde 404.** Lo
hacía, y era engañoso: dar de baja es dejar de consultarlo, no perder lo que se
sabía de él, sus `.rsc` siguen en el repositorio a propósito, y de hecho se
acababa de llegar desde `/cambios`, donde ese equipo sale listado. Lo único que
ya no existe es su ficha del inventario, que es de donde sale la ruta de sus
archivos. Ahora se vuelve a `/cambios` explicándolo — que es de donde se venía y
donde sus versiones se siguen viendo una a una — en vez de dar un error por algo
que no lo es.

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

`/ajustes` (permiso `ajustes.ver` para abrirla) son **siete tarjetas**, y cada
una aparece solo si la cuenta tiene su permiso. Se toca todo **sin reiniciar
nada y sin reescribir ninguna unidad de systemd**:

1. **Cada cuánto se respalda** (`ajustes.programador`). El intervalo **general**
   del programador
   (`intervalo_minutos`, el que heredan los equipos que no traen el suyo) y si
   se respalda nada más arrancar (`al_arrancar`). El programador relee los
   ajustes mientras espera, así que bajar de 4 horas a 30 minutos surte efecto
   en el momento y no dentro de 4 horas, que es justo cuando alguien está
   esperando a ver si funcionó. En esa misma tarjeta está **Respaldar ahora**
   (`ajustes.respaldar`), en su propio formulario y no como un segundo botón del
   otro: pedir un respaldo no debe guardar de paso un intervalo que se estaba
   tecleando a medias, ni al revés, y además son permisos distintos.
2. **Las credenciales SSH generales** (`ajustes.ssh`). Usuario y clave con los
   que se entra a
   cualquier equipo que no traiga las suyas — lo que hay que cambiar cuando se
   rota la clave de la flota. El usuario no puede quedar vacío; la clave vacía
   significa "no la toques", porque guardar la cadena vacía dejaría a la flota
   entera sin credencial. La clave **no se manda al navegador ni para rellenar
   el campo**, pero sí se dice si hay alguna puesta: un campo vacío sin más no
   distingue "no la toques" de "no hay ninguna".
3. **El nombre de los routers** (`equipos.identidades`): preguntarle su
   `/system identity` a toda la flota de una vez. Ver
   [Preguntarle el nombre a toda la flota](#preguntarle-el-nombre-a-toda-la-flota).
4. **A dónde se suben los respaldos** (`ajustes.remoto`). La dirección del
   repositorio remoto, la
   rama, cada cuántos ciclos con cambios se sube, el usuario y el token, con un
   botón para probar la conexión en el momento. Tiene sección propia: ver
   [Subir los respaldos fuera del servidor](#subir-los-respaldos-fuera-del-servidor).
5. **Mudarse a otro servidor** (`datos.mudanza`, y además alcance total).
   Descargar el servidor entero en un `.tar.gz` y volcar aquí uno de otra
   máquina. Va justo detrás del remoto a propósito: los dos hablan de sacar una
   copia de esta máquina, y quien está pensando en "qué pasa si pierdo el
   servidor" tiene las dos respuestas seguidas — el remoto contesta "conservo el
   histórico" y esto, "levanto el sistema entero en otro sitio", que no es lo
   mismo y suele confundirse. Tiene sección propia: ver
   [Mudarse a otro servidor](#mudarse-a-otro-servidor).
6. **El fondo de la pantalla de entrada** (`ajustes.fondo`, ver arriba).
7. **Borrar los datos de un equipo** (`datos.borrar`), a lo ancho y fuera del
   mosaico porque lleva una tabla de cinco columnas. Ver
   [Borrar los datos de un equipo](#borrar-los-datos-de-un-equipo).

Con `ajustes.ver` y nada más la pantalla se abre pero no hay ningún formulario
que rellenar, y **se dice**: una página en blanco se lee como un panel roto, y
lo que falta ahí son permisos que alguien tiene que dar.

Lo que se cambia en el panel **no se escribe en el YAML**: va a
`almacen.ajustes` (`/root/mkbackup/ajustes.json` por defecto) y **manda sobre
`config.yaml`**. Si cambias algo en el YAML y no ves el efecto, es que hay un
ajuste guardado desde el panel.

**Por qué un archivo aparte y no `config.yaml`:** las dos unidades corren con
`ProtectSystem=strict`, y `config.yaml` es un archivo que **edita una persona
con root**. Está lleno de comentarios que un volcado automático se comería, y
lleva las rutas del repositorio y los parámetros del propio panel. Darle permiso
de escritura sobre él al proceso que atiende peticiones HTTP sería darle la
capacidad de apuntar los respaldos a otro sitio. Lo que el panel puede tocar es
una **lista blanca de diez claves** (`AJUSTES_EDITABLES` en `config.py`), y todo
lo demás sigue siendo de archivo:

| Clave | A dónde va |
|---|---|
| `intervalo_minutos`, `al_arrancar` | `planificador` |
| `ssh_usuario`, `ssh_password` | `ssh` |
| `remoto`, `remoto_rama`, `remoto_usuario`, `remoto_token`, `remoto_cada` | `almacen` |
| `fondo_login` | `web` |

Eran cinco. Las cinco del repositorio remoto se añadieron por una razón que se
puede decir en una línea: **un token caduca y se rota**, igual que la clave de
la flota, y exigir un root, un SSH y un editor para eso lleva a que no se haga —
o a que la copia de fuera se quede fallando meses. Las **rutas locales**, en
cambio, siguen siendo de archivo, y la diferencia no es de importancia sino de
naturaleza: cambiar a dónde se replica es apuntar una copia adicional, mientras
que apuntar los respaldos a otro sitio del disco sería **dejar de guardarlos
donde todo el mundo cree que están**, sin que nada lo delate.

Las credenciales SSH **sí** están en esa lista, y el precio hay que decirlo
claro: **quien entre al panel como administrador puede cambiar con qué
credenciales se entra a los routers, y a qué máquina de internet salen las
configuraciones.** Se aceptó porque pedir un root y un editor para rotar la
clave de la flota llevaba a que no se rotara nunca. El archivo de ajustes se
escribe con `0600` por eso mismo, y el cambio queda en la auditoría — el usuario
y la dirección del remoto sí, la clave y el token nunca.

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

- **No restaura equipos.** Ni el `.rsc` ni el `.backup`. Eso es a mano, con la
  cabeza puesta y mirando la versión de RouterOS. (Restaurar el **servidor** sí
  se puede desde el panel; es otra cosa, y está en
  [Mudarse a otro servidor](#mudarse-a-otro-servidor).)
- **No edita las rutas ni la configuración general.** Solo la lista blanca de
  `AJUSTES_EDITABLES`. Las rutas locales —el repositorio, los binarios, el
  inventario— y Telegram (`telegram.token`, `telegram.chat_id` y
  `telegram.modo`: `resumen`, `detalle` o `ninguno`) viven en `config.yaml`, que
  se toca con un editor y con root. El repositorio **remoto** sí se configura
  desde el panel, y por qué esa distinción está explicado en
  [Ajustes desde el panel](#ajustes-desde-el-panel). El panel escribe únicamente
  bajo `/root/mkbackup`, pero ahí dentro escribe **casi todo**: el inventario,
  los ajustes, las cuentas, la auditoría, la imagen de fondo del login, los
  `.tar.gz` que se suben para restaurar y —con `datos.mudanza`— **el contenido
  entero de esa carpeta**, porque volcar un paquete reemplaza el servidor
  completo, `config.yaml` incluido. Ver
  [Mudarse a otro servidor](#mudarse-a-otro-servidor).
- **No enseña los secretos de las configuraciones.** El diff los tapa (ver
  arriba). Se ve qué cambió, no lo que dice.
- **No tiene HTTPS propio, ni API REST, ni 2FA, ni recuperación de clave por
  correo.** Hay un `/api/estado` con el mismo JSON que pinta la pantalla de
  estado y un `/api/identidades` con el avance del sondeo de nombres, y nada
  más. Una clave olvidada se arregla desde el panel por otro administrador, o
  con `--clave-usuario`.

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
exige sesión: `/` redirige al formulario y todo lo que cuelga de `/api/` —el
JSON crudo, si prefieres consumirlo desde otro sitio— responde **401** para que
un script sepa que le falta login en vez de tragarse el HTML del login como si
fuera el estado.

---

## Subir los respaldos fuera del servidor

Mientras el único sitio donde están los respaldos sea este servidor, esto no es
una copia de seguridad: es un disco. Por eso `mkbackup` puede empujar el
repositorio de configuraciones a un **repositorio git remoto** al terminar
cualquier ciclo que haya encontrado algo nuevo.

Replicar ya se podía antes, pero **solo editando `config.yaml` con root**, que
en la práctica significa que se configuraba el día de la instalación o no se
configuraba nunca. Ahora se pone desde **Ajustes**: dirección, rama, cada
cuántos ciclos con cambios se sube, usuario y token.

> **⚠ Ese repositorio tiene que ser PRIVADO.**
>
> Con `export.mostrar_secretos: true` —que es el valor **por defecto**— los
> `.rsc` llevan dentro las **contraseñas PPPoE, las PSK de wifi y los secretos
> de VPN de tus clientes, en texto plano**. Lo que se sube ahí no es "un
> respaldo de configuraciones": son las credenciales de la red de otra gente.
>
> Un repositorio público, o uno privado de una cuenta compartida con media
> oficina, es repartirlas. Si el remoto no puede ser privado de verdad, las dos
> únicas salidas son un **remoto cifrado** (más abajo) o `mostrar_secretos:
> false`, sabiendo que entonces el respaldo ya no sirve para restaurar
> credenciales.

| Ajuste | Qué es |
|---|---|
| **Dirección del repositorio** | `https://…`, `ssh://…` o `git@servidor:empresa/repo.git`. Vacío = no se sube a ningún sitio. |
| **Rama** | La rama del remoto. Vacío = la misma que se usa aquí. |
| **Cada cuántos ciclos con cambios** | `1` = en cuanto hay algo nuevo. `0` = no subir, sin borrar la dirección. |
| **Usuario** | Casi nunca importa —GitHub y GitLab miran el token— pero hay servidores que sí. Vacío = `x-access-token`. |
| **Token de acceso** | La credencial de un repositorio privado por HTTPS. Por SSH se deja vacío: ahí manda la llave del servidor. |

**Solo se admiten `https://`, `ssh://` y `git@…`.** Nada de `http://` sin
cifrar, y nada de rutas locales ni de esquemas raros. La dirección la escribe un
administrador, pero es lo que decide **a qué máquina de internet salen las
configuraciones de los clientes con sus claves dentro**: una lista de esquemas
conocidos es barata y cierra la puerta a que un descuido —o una cuenta del panel
comprometida— lo mande a otro sitio por un protocolo que nadie esperaba.

El token, como la clave de los routers, **no vuelve al navegador ni para
rellenar el campo**: vacío significa "deja el que hay". Si se guardara la cadena
vacía, abrir esta pantalla y pulsar Guardar dejaría la subida sin credencial y
fallando en silencio. Borrar la dirección **sí** borra el token: una credencial
que ya no sirve para nada es un secreto guardado sin motivo.

### El token no va dentro de la URL

Lo habitual es meter la credencial en la propia dirección
(`https://usuario:token@github.com/…`). Aquí **no**, y la razón es concreta: esa
URL se guarda en `.git/config`, o sea que queda **escrita en el disco**; viaja
en **cada copia del repositorio** que alguien haga; y sale tal cual en el
**mensaje de error de cualquier fallo de red**, que es el mensaje que acaba en
el log, en el panel y en el aviso de Telegram.

En vez de eso, la credencial se pasa como cabecera `http.extraheader` en la
línea de comandos de **esa** orden de git concreta (`Almacen._credencial` en
`store.py`) y **muere con ella**. No queda escrita en ningún lado.

Y como la credencial viaja en los argumentos de git, los errores de git **no se
vuelcan tal cual**: pasan por un filtro que sustituye el token —y su versión
codificada— por `«oculto»` antes de que el mensaje salga a ningún sitio
(`_git(..., tapar=…)`). Un token que se filtra por el mensaje de error de la
operación que lo usa es la forma clásica de perderlo.

Un detalle que parece menor y no lo es: la subida corre con
`GIT_TERMINAL_PROMPT=0` (y `GIT_ASKPASS`/`SSH_ASKPASS` vacíos). Sin eso, un
remoto que pide credenciales deja el proceso **parado esperando a alguien que no
hay**. En un servicio desatendido, un push que se cuelga es peor que un push que
falla: el que falla se ve.

### "Probar la conexión"

Al lado del formulario hay un botón que hace un `git ls-remote --heads`:
pregunta qué ramas tiene el remoto y **no escribe nada**. Contesta con la lista
de ramas, o con "se llega al repositorio, y está vacío (el primer push lo
llena)", o con el motivo del fallo ya tapado.

Existe porque la alternativa para saber si el token vale era **esperar al
próximo ciclo con cambios y leer el log**, y quien acaba de pegar un token
quiere saberlo ahora. Cada prueba, salga bien o mal, queda en la auditoría.

### Cada cuántos ciclos, y los pendientes

`remoto_cada` agrupa varios ciclos en un solo push. Con una flota grande y un
remoto lento tiene sentido, y **no se pierde nada**: lo que se sube es el
histórico entero, así que juntar tres ciclos en un push deja el remoto
exactamente igual que hacer tres. `0` apaga la subida sin borrar la dirección,
que es lo que quieres para pararla un rato sin tener que volver a pegar el token
después.

Los ciclos que se saltan **se cuentan**, y el contador se enseña. Un contador
parado en 0 y uno en 40 son dos situaciones muy distintas y desde fuera se verían
igual. Si un push falla, los pendientes **no se ponen a cero**: esos commits no
salieron de aquí, y la próxima vez hay que volver a intentarlo aunque no toque
por contador.

Los **renombrados también cuentan como cambio**. Si solo se replicara cuando
cambia una configuración, el remoto se quedaría con los nombres viejos de los
equipos que se identificaron solos.

### El resultado de la última subida se ve en el panel

Cómo fue la última subida se guarda en **`replica.json`**, junto al archivo de
estado, y Ajustes lo enseña: cuándo, si salió bien, el detalle —o el error de
git, ya tapado— y cuántos ciclos con cambios quedan sin subir.

No es adorno. Sin eso, saber si los respaldos están llegando al remoto exige
entrar por SSH al servidor y leer el journal, o sea que no lo mira nadie. Y
**una copia fuera que lleva tres semanas fallando y nadie lo sabe es lo mismo
que no tener copia fuera**, con el agravante de que se cree que sí. Un push
fallido va además al log y a la lista de fallos del ciclo, así que sale por
Telegram como un equipo más que no salió bien.

Escribir `replica.json` **nunca puede tumbar un ciclo**: si no se puede escribir,
se avisa en el log y el respaldo sigue.

### Si no puede ser privado: remoto cifrado

Aunque el repositorio sea privado, replicarlo a GitHub pone las credenciales de
toda tu red en un tercero. Si eso no es aceptable —y con secretos dentro hay
motivos para que no lo sea— la salida es un remoto cifrado:

```bash
apt install git-remote-gcrypt
gpg --batch --gen-key                    # clave dedicada, como root
# en config.yaml:
#   remoto: "gcrypt::git@github.com:usuario/repo-privado.git"
```

`git-remote-gcrypt` cifra los objetos antes de subirlos: el remoto ve blobs
opacos. **Guarda la clave GPG fuera del servidor** — sin ella el mirror es
ilegible, y un mirror que no se puede descifrar no es un respaldo.

Esta dirección **se pone en `config.yaml` con root, no desde el panel**:
`gcrypt::…` no empieza por ninguno de los tres esquemas que el panel acepta, y
la lista de esquemas no se abre para esto. Es coherente con lo que es: un remoto
cifrado exige además una clave GPG en el servidor, o sea que ya hace falta
entrar por SSH. Lo que se configura desde la web es el caso normal.

---

## Mudarse a otro servidor

**Esto no es otro respaldo más.** Los respaldos que hace este programa son de la
configuración de los **routers**; lo de aquí es la copia del **propio
servidor**: el histórico completo, el inventario, las cuentas del panel, los
ajustes y el registro de auditoría. Sin ello, cambiar de máquina significa
volver a dar de alta la flota a mano y empezar el historial de cero, que es
exactamente lo que este proyecto existe para no tener que hacer.

Es también otra cosa que el [repositorio
remoto](#subir-los-respaldos-fuera-del-servidor), y se confunden a menudo: el
remoto contesta a "conservo el histórico si se muere el disco", y esto a
"levanto el sistema entero en otro sitio".

### Qué va dentro, y por qué va todo

La tentación es dejar fuera lo que se puede reconstruir. No se hace: una copia
que hay que completar a mano no es una mudanza, es una lista de tareas. Y la
mitad de lo "reconstruible" no lo es en la práctica — las claves de los routers
del inventario, quién tenía qué permiso, cuándo empezó a fallar un equipo.

| Pieza | Qué es | ¿Esencial? |
|---|---|---|
| `config.yaml` | La configuración: rutas, credenciales SSH y Telegram | **sí** |
| `inventory.csv` | El inventario: la flota entera con sus credenciales | **sí** |
| `usuarios.json` | Las cuentas del panel, sus roles y su alcance | **sí** |
| `configs.git/` | **El histórico**: todas las versiones de todos los equipos | **sí** |
| `ajustes.json` | Lo que se cambió desde el panel | no |
| `estado.json` | Cómo fue el último ciclo | no |
| `equipos.json` | Lo último que se supo de cada equipo: modelo y versión | no |
| `programador.json` | Cuándo toca el próximo ciclo y cuándo se vio cada equipo | no |
| `replica.json` | Cómo fue la última subida al repositorio remoto | no |
| `auditoria.log` | Quién entró y quién tocó qué | no |
| La imagen de fondo | La pantalla de entrada, con su nombre real | no |
| `binarios/` | Los `.backup` binarios de cada equipo | no |

La lista es `piezas()` en `mkbackup/mudanza.py`, y devuelve **también las que
no existen todavía**: la pantalla tiene que poder decir "esto no está" en vez de
callárselo. Un paquete al que le falta el archivo de cuentas deja el panel sin
usuarios al restaurar, y eso hay que verlo **antes** de descargarlo, no después
de mudarse. Lo que no existe no se mete y no es un error —un servidor recién
instalado no tiene `replica.json` ni imagen de fondo—, pero si lo que falta es
esencial se avisa.

> **Ese archivo es la pieza más peligrosa que produce este programa.** Lleva las
> claves SSH de la flota **en claro** (así las guarda el inventario), los hashes
> de las claves del panel y, si `export.mostrar_secretos` está puesto —que es lo
> normal—, las **passwords PPPoE y las PSK de wifi de tus clientes** dentro del
> histórico. **Quien lo tiene, tiene la red.** Trátalo como tratarías el
> inventario: se crea con permisos `0600` y el temporal nace ya privado, sin
> ventana; cada descarga queda en la auditoría como `mudanza`, que es lo único
> que convierte "alguien se llevó una copia" en un dato y no en una sospecha; y
> bórralo en cuanto llegue a su destino.

### Descargarla

Desde el panel, en la tarjeta **Mudarse a otro servidor** de `/ajustes`, con el
permiso `datos.mudanza` **y alcance total** (no existe la copia de un solo
cliente: ver [Por qué `datos.mudanza` va aparte](#por-qué-datosmudanza-va-aparte)).
La pantalla enseña antes qué se llevaría y cuánto pesa **en crudo** — el
`.tar.gz` sale bastante más pequeño, porque el histórico son textos de
configuración y comprimen mucho.

O por terminal, que es lo que se usa en un cron o desde otra máquina:

```bash
.venv/bin/python -m mkbackup.cli -c /root/mkbackup/config.yaml \
    --exportar-datos /mnt/usb/
```

Si la ruta es **una carpeta que ya existe**, el paquete se escribe dentro con el
nombre por defecto (`mkbackup-<servidor>-<fecha>.tar.gz`): dar una carpeta es lo
que se teclea sin pensar, y fallar con un "es un directorio" no ayuda a nadie.
Guardarlo **dentro** de lo que está empaquetando (el repositorio, los binarios)
sí se rechaza: el tar se leería a sí mismo mientras crece, y el síntoma —un
archivo enorme y una máquina sin disco— no se parece en nada a la causa.

Con un ciclo de respaldo en marcha, tanto el panel como el terminal se niegan o
descartan el resultado: el programador es **otro proceso** commiteando en el
mismo repositorio, y un paquete hecho a mitad de un commit lleva el árbol de git
incompleto. Lo peor es que no lo parece —el CRC y los sha256 cuadran, porque se
calculan sobre lo que se copió—, así que se restaura sin una queja y los objetos
aparecen sin la referencia que los nombra.

### Restaurarla

**Con los servicios parados** (`mkbackup.service` y el panel). Restaurar por
debajo de un ciclo en marcha reescribe el repositorio que ese ciclo está usando,
y lo que queda no es ni lo viejo ni lo nuevo:

```bash
systemctl stop mkbackup mkbackup-web

.venv/bin/python -m mkbackup.cli -c /root/mkbackup/config.yaml \
    --restaurar-datos /mnt/usb/mkbackup-viejo-20260728-1130.tar.gz
```

Antes de tocar nada se dice **de qué servidor viene el paquete, de cuándo es y
con qué versión se hizo**: equivocarse de archivo se nota aquí leyendo una
línea, y no después, viendo el inventario de otro cliente en el panel.

**Sin `--sobrescribir` no se pisa nada.** Si en la máquina ya hay datos, la
orden se para con el disco intacto y dice cuáles estorban. Para reemplazarlos de
verdad:

```bash
.venv/bin/python -m mkbackup.cli -c /root/mkbackup/config.yaml \
    --restaurar-datos /mnt/usb/mkbackup-viejo-20260728-1130.tar.gz --sobrescribir
```

Y aun así **lo viejo no se borra**: se aparta con el sufijo
`.antes-de-restaurar-<fecha>` al lado. Ocupa el doble un rato, y a cambio
equivocarse de paquete se deshace.

Todo se hace en dos tiempos. Primero se extrae **entero** a una carpeta de paso,
comprobando los sha256 por el camino, y solo si el paquete estaba completo se
empieza a colocar nada. Extraer encima de los datos buenos y descubrir a mitad
que el archivo estaba truncado deja el servidor sin lo viejo y sin lo nuevo, que
es el peor resultado posible de una restauración. Si aun así algo falla al
colocar las piezas, lo apartado vuelve a su sitio en orden inverso y el servidor
se queda como estaba.

Al terminar avisa si el `config.yaml` restaurado apunta a rutas distintas de las
que se acaban de usar: los datos se colocan donde dice la configuración de
**esta** máquina, y acto seguido se restaura la del origen, que manda a partir
del próximo arranque. Con la distribución de siempre (`/root/mkbackup`)
coinciden y no hay nada que decir; si no, el sistema arrancaría mirando carpetas
vacías y parecería que la restauración no hizo nada.

### Restaurar desde el panel, y por qué el terminal no sobra

También se puede desde `/ajustes`, y va en **dos peticiones**: se sube el
archivo, se ve **de qué servidor viene y de cuándo es**, y solo entonces se
confirma escribiendo **`RESTAURAR`** en un campo. Sustituir la flota entera por
la de otro momento tiene que poder pararse leyendo una línea. (Son dos también
por una razón práctica: el formulario lleva un único campo, así que el cuerpo se
vuelca a disco según llega en vez de juntarlo en memoria — el histórico de un
cliente grande son cientos de megas y este servidor no tiene ni swap.)

Al terminar **se cierran todas las sesiones**, incluida la de quien acaba de
hacerlo. El archivo de cuentas es otro: los tokens que quedaban en memoria
pertenecen a usuarios que quizá ya no existen, o que existen con otro rol y otro
alcance, y dejarlos vivos sería conservar unos permisos que ya no dice nadie.

Aun así, **en una máquina nueva el panel no sirve**, y ese es el caso que de
verdad importa: allí todavía no hay ninguna cuenta con la que entrar, porque las
cuentas están justo dentro del paquete que se quiere restaurar. Por eso
`--restaurar-datos` no es un extra sino el camino principal, y el panel escribe
la orden exacta en pantalla.

### Dentro del `.tar.gz`, nombres lógicos

El paquete lleva un `manifiesto.json` y las piezas al lado, con nombres
**lógicos y relativos** (`usuarios.json`, `configs.git/…`), **nunca rutas
absolutas**. Importa por dos cosas:

- Al restaurar, cada pieza va **a donde diga la configuración de la máquina de
  destino**, no a donde estaba en la de origen. Eso permite mudarse entre
  servidores con distinta distribución de carpetas.
- Y quita de en medio la familia entera de agujeros de "extraer un tar te
  escribe en `/etc`". Un nombre que empiece por `/`, que lleve `..` o que traiga
  una unidad de Windows se rechaza, igual que un enlace simbólico, un
  dispositivo o un fifo: nada de eso tiene una razón legítima de estar dentro, y
  esto se extrae **como root**. Tampoco se da por buena la cuenta de bytes que
  declara el manifiesto — se corta **mientras** se escribe, porque la gracia de
  una bomba de descompresión es justo que lo que ocupa se sabe cuando ya se
  escribió.

Los usuarios y grupos del origen se borran del tar (todo entra como `root`, con
`0600` y `0700`): no le sirven de nada al destino y, en un archivo que va a
viajar, son información de más sobre el servidor.

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
├── identidades.py  Preguntarle su nombre a un router o a la flota entera
├── imagen.py       Ajuste de la imagen de fondo del login al subirla
├── mudanza.py      Llevarse el servidor entero a otra máquina
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
| `replica.json` | Cómo fue la última subida al remoto | el respaldo |
| `programador.json` | Cuándo se consultó cada equipo | el programador |
| `equipos.json` | Modelo, versión y último respaldo bueno | el respaldo |
| `ajustes.json` | Lo que se cambia desde el panel | el panel |
| `usuarios.json` | Cuentas, roles y hashes | el panel |
| `auditoria.log` | Un evento por línea | el panel |
| `fondo-login.<ext>` | La imagen de fondo del login, si se subió una | el panel |

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
python -m tests.test_device       # clasificacion de fallos, export, credenciales
python -m tests.test_paginas      # el HTML servido, bien formado (CSS, JS, escapado)
python -m tests.test_web          # de donde viene la peticion y cuanto aguanta
python -m tests.test_panel        # el panel levantado: cada formulario se envia
python -m tests.test_imagen       # ajuste del fondo, metadatos y bombas de zip
python -m tests.test_identidades  # el sondeo de nombres: credenciales e historial
python -m tests.test_datos        # borrar lo de un equipo de baja, sobre git de verdad
python -m tests.test_remoto       # la subida al remoto, y que el token no se escape
python -m tests.test_mudanza      # empaquetar y restaurar el servidor entero
```

**1662 comprobaciones** entre los dieciocho archivos, todas en verde hoy. No
requieren ningún equipo ni red: `test_device` levanta un socket local que
acepta la conexión y la cierra sin hablar, que es exactamente lo que hace un
MikroTik con la lista de direcciones puesta.

`test_paginas` mira la **estructura** del HTML que sirve el panel, no su
contenido: llaves de CSS cuadradas (una de más apaga media hoja de estilos y el
navegador no avisa), ningún `const` repetido en el ámbito de fuera (es
`SyntaxError`, y deja la página en blanco), nada servido después de `</html>` y
el escapado de lo que escribe una persona. Las tres cosas han roto este panel
alguna vez con el servidor contestando 200.

**Aquí había una tabla con las comprobaciones de cada archivo.** Se quitó a
propósito: ese número solo se sabe **ejecutando** las pruebas, porque cuenta las
llamadas a `comprobar()` que de verdad corren, y una tabla que se copia de la
ejecución de hace tres meses envejece sin que se note. El total de arriba se
comprueba de una vez cuando se lanzan todas; los repartos por archivo, no. Mejor
sin números que con números inventados. Lo que cubre cada archivo está en el
comentario de su línea, arriba.

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

# 3 líneas: "Repositorio de configuraciones" (el commit que crea el repo),
# el "Alta:" y el "Cambio:". Solo dos son respaldos: las ejecuciones sin
# cambios no dejan nada.
git -C datos/configs.git log --oneline
```

Ese `.dns_simulado` cambia la configuración que sirve el simulador en caliente,
para provocar un cambio real sin reiniciarlo.

**El simulador no implementa SFTP**, así que hay que usar `--sin-binario`: la
descarga del `.backup` solo se puede probar contra un equipo real.

Detalle: el simulador **no** usa `SO_REUSEADDR` a propósito. En Windows eso
permite que dos procesos bindeen el mismo puerto, y un simulador viejo quedándose
con el tráfico hace que las pruebas "pasen" contra la versión equivocada del
código. Si el puerto está ocupado, falla y lo dice.
