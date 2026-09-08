"""Rutas, cola persistente y utilidades compartidas por el plugin y la CLI.

Este módulo lo importan dos procesos distintos:
  - el plugin, dentro de Nicotine+ (que en Linux puede ir confinado en un
    snap o en un flatpak, y en Windows y macOS corre con su propio Python)
  - la CLI `flacpipe` y la interfaz, fuera

Por eso todas las rutas son absolutas y toda escritura de la cola pasa por un
bloqueo sobre `cola.lock` (flock en Linux/macOS, msvcrt.locking en Windows).

Dónde está la biblioteca se decide en este orden:
  1. la variable de entorno FLACPIPE_BIBLIOTECA;
  2. `flacpipe.json`, junto a este archivo (lo escribe instalar.py, y es lo
     único que ve el plugin cuando corre confinado);
  3. la carpeta de música del sistema: xdg-user-dir MUSIC en Linux, ~/Music en
     macOS y Windows.
"""

import errno
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime

ES_WINDOWS = sys.platform == "win32"
ES_MAC = sys.platform == "darwin"
ES_LINUX = not ES_WINDOWS and not ES_MAC

if ES_WINDOWS:
    import msvcrt
else:
    import fcntl

DIR_CODIGO = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CONFIG = os.path.join(DIR_CODIGO, "flacpipe.json")


# ─── Configuración ────────────────────────────────────────────────────────────

def leer_config():
    """El flacpipe.json que deja el instalador, o {} si no hay."""
    try:
        with open(ARCHIVO_CONFIG, encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


CONFIG = leer_config()


def casa():
    """El HOME de verdad. Dentro del snap $HOME es ~/snap/nicotine-plus/<rev>,
    pero snapd deja el real en SNAP_REAL_HOME."""
    return os.environ.get("SNAP_REAL_HOME") or os.path.expanduser("~")


def musica_por_defecto():
    """La carpeta de música del sistema, sin mirar la config."""
    base = casa()
    if ES_LINUX:
        # xdg-user-dir MUSIC, leído a mano para no depender de un ejecutable
        try:
            with open(os.path.join(base, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
                for linea in f:
                    m = re.match(r'^\s*XDG_MUSIC_DIR\s*=\s*"?(.*?)"?\s*$', linea)
                    if m:
                        ruta = m.group(1).replace("$HOME", base)
                        if os.path.isdir(ruta):
                            return ruta
        except OSError:
            pass
        for nombre in ("Música", "Music", "Musica"):
            ruta = os.path.join(base, nombre)
            if os.path.isdir(ruta):
                return ruta
    return os.path.join(base, "Music")


def _raiz_biblioteca():
    ruta = os.environ.get("FLACPIPE_BIBLIOTECA") or CONFIG.get("biblioteca") or musica_por_defecto()
    return os.path.normpath(os.path.expanduser(ruta))


# ─── Rutas ────────────────────────────────────────────────────────────────────
# En Linux el snap ve la biblioteca porque la interfaz `home` permite
# @{HOME}/[^.]*/** — o sea, carpetas ocultas DENTRO de Música sí, pero no
# carpetas ocultas colgando directamente de $HOME. Por eso .pipeline y
# .descargas viven dentro de la biblioteca y no en ~/.config.
RAIZ_BIBLIOTECA = _raiz_biblioteca()
DIR_PIPELINE = os.path.join(RAIZ_BIBLIOTECA, ".pipeline")
DIR_DESCARGAS = os.path.join(RAIZ_BIBLIOTECA, ".descargas")
ARCHIVO_COLA = os.path.join(DIR_PIPELINE, "cola.json")
ARCHIVO_LOCK = os.path.join(DIR_PIPELINE, "cola.lock")
ARCHIVO_LOG = os.path.join(DIR_PIPELINE, "pipeline.log")

# Carpetas de primer nivel de la biblioteca que NO son artistas.
# "Artistas" es la biblioteca vieja de Yahel; se deja intacta a propósito.
# El resto son cosas que iTunes/Música/Windows dejan en ~/Music.
NO_SON_ARTISTAS = {
    ".pipeline", ".descargas", "Artistas", "lost+found",
    "iTunes", "Music Library.musiclibrary", "Playlists", "Audio Music Apps",
    "GarageBand", "Logic", "Media.localized", "Ableton",
} | set(CONFIG.get("ignorar") or [])

# ─── Estados de una petición ──────────────────────────────────────────────────
PENDIENTE = "pendiente"
BUSCANDO = "buscando"
DESCARGANDO = "descargando"
LISTO = "listo"
FALLIDO = "fallido"
CANCELADO = "cancelado"

EN_CURSO = {BUSCANDO, DESCARGANDO}
ACTIVOS = {PENDIENTE} | EN_CURSO
TERMINADOS = {LISTO, FALLIDO, CANCELADO}

MAX_LOG_BYTES = 1_000_000


def ahora():
    return datetime.now().isoformat(timespec="seconds")


def asegurar_dirs():
    for d in (DIR_PIPELINE, DIR_DESCARGAS):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise


def registrar(mensaje):
    """Escribe una línea en el log del pipeline. Nunca lanza."""
    try:
        asegurar_dirs()
        if os.path.exists(ARCHIVO_LOG) and os.path.getsize(ARCHIVO_LOG) > MAX_LOG_BYTES:
            with open(ARCHIVO_LOG, "r", encoding="utf-8", errors="replace") as f:
                cola_texto = f.readlines()[-2000:]
            with open(ARCHIVO_LOG, "w", encoding="utf-8") as f:
                f.writelines(cola_texto)
        with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ahora()}  {mensaje}\n")
    except Exception:
        pass


# ─── Cola con bloqueo ─────────────────────────────────────────────────────────

def _intentar_bloquear(f):
    """Bloqueo exclusivo no bloqueante. True si se consiguió."""
    try:
        if ES_WINDOWS:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _liberar(f):
    if ES_WINDOWS:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class bloqueo:
    """Context manager: bloqueo exclusivo sobre cola.lock."""

    def __init__(self, timeout=20):
        self.timeout = timeout
        self._f = None

    def __enter__(self):
        asegurar_dirs()
        self._f = open(ARCHIVO_LOCK, "a+")
        limite = time.monotonic() + self.timeout
        while not _intentar_bloquear(self._f):
            if time.monotonic() > limite:
                self._f.close()
                self._f = None
                raise TimeoutError(f"No se pudo bloquear {ARCHIVO_LOCK}")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        try:
            _liberar(self._f)
        except OSError:
            pass
        finally:
            self._f.close()
            self._f = None
        return False


def _cola_vacia():
    return {"version": 1, "peticiones": []}


def leer_cola():
    """Lee la cola sin bloquear (para lecturas informativas)."""
    try:
        with open(ARCHIVO_COLA, "r", encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        return _cola_vacia()
    if not isinstance(datos, dict) or "peticiones" not in datos:
        return _cola_vacia()
    return datos


def escribir_cola(cola):
    """Escritura atómica: temporal en el mismo directorio + os.replace.

    En Windows el replace falla si otro proceso tiene cola.json abierto en ese
    instante (la interfaz lo lee cada pocos segundos), así que se reintenta.
    """
    asegurar_dirs()
    tmp = ARCHIVO_COLA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cola, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for intento in range(20):
        try:
            os.replace(tmp, ARCHIVO_COLA)
            return
        except PermissionError:
            if intento == 19:
                raise
            time.sleep(0.05)


def modificar_cola(funcion):
    """Lee-modifica-escribe la cola bajo bloqueo. `funcion(cola)` devuelve el resultado."""
    with bloqueo():
        cola = leer_cola()
        resultado = funcion(cola)
        escribir_cola(cola)
    return resultado


# ─── Peticiones ───────────────────────────────────────────────────────────────

def _slug(texto, maximo=40):
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto[:maximo] or "peticion"


def separar_consulta(texto):
    """'Radiohead - OK Computer' -> ('Radiohead', 'OK Computer').

    Acepta ' - ', ' – ' y ' — ' como separador. Si no hay separador, todo el
    texto es la consulta y no sabemos artista ni álbum por adelantado.
    """
    for sep in (" — ", " – ", " - "):
        if sep in texto:
            artista, _, album = texto.partition(sep)
            return artista.strip(), album.strip()
    return None, None


def crear_peticion(consulta, tipo="album", artista=None, album=None, lista=None):
    consulta = " ".join(consulta.split())
    if artista is None and album is None:
        artista, album = separar_consulta(consulta)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "id": f"{marca}-{_slug(consulta)}",
        "consulta": consulta,
        "tipo": tipo,                 # "album" | "cancion"
        "lista": lista,               # de qué playlist vino, si vino de una
        "artista": artista,
        "album": album,
        "estado": PENDIENTE,
        "creado": ahora(),
        "actualizado": ahora(),
        "intentos": 0,
        "candidato": None,
        "rechazados": [],             # [[usuario, carpeta], ...]
        "progreso": None,
        "resultado": None,
        "mensaje": "",
    }


def añadir_peticiones(consultas, tipo="album", lista=None):
    """Añade consultas a la cola, ignorando las que ya estén activas. Devuelve las creadas."""
    creadas = []

    def _op(cola):
        activas = {
            p["consulta"].casefold()
            for p in cola["peticiones"] if p["estado"] in ACTIVOS
        }
        for consulta in consultas:
            consulta = " ".join(consulta.split())
            if not consulta or consulta.casefold() in activas:
                continue
            pet = crear_peticion(consulta, tipo=tipo, lista=lista)
            cola["peticiones"].append(pet)
            activas.add(consulta.casefold())
            creadas.append(pet)

    modificar_cola(_op)
    for pet in creadas:
        registrar(f"[cola] + {pet['id']}  ({pet['consulta']})")
    return creadas


def buscar_peticion(cola, id_pet):
    for pet in cola["peticiones"]:
        if pet["id"] == id_pet:
            return pet
    return None


def formato_bytes(n):
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unidad == "TB":
            return f"{n:.1f} {unidad}" if unidad != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# ─── Dónde está Nicotine+ ─────────────────────────────────────────────────────
# pynicotine/config.py decide sus carpetas así:
#   Windows:  %APPDATA%\nicotine            (config en …\nicotine\config\config)
#   resto:    ~/.nicotine si existe (antiguo); si no, $XDG_CONFIG_HOME/nicotine y
#             $XDG_DATA_HOME/nicotine. El snap y el flatpak redirigen esas
#             variables a su propia carpeta, y eso es lo que las distingue.

NOMBRES_NICOTINE = {"nicotine", "nicotine-plus", "nicotine+"}
FLATPAK_ID = "org.nicotine_plus.Nicotine"


def _candidatos_nicotine():
    """[(sabor, archivo de config, carpeta de datos)] que podría haber aquí."""
    base = casa()
    if ES_WINDOWS:
        datos = os.path.join(os.environ.get("APPDATA")
                             or os.path.join(base, "AppData", "Roaming"), "nicotine")
        return [("windows", os.path.join(datos, "config", "config"), datos)]

    lista = []
    snap = os.path.join(base, "snap", "nicotine-plus", "current")
    lista.append(("snap", os.path.join(snap, ".config", "nicotine", "config"),
                  os.path.join(snap, ".local", "share", "nicotine")))
    flatpak = os.path.join(base, ".var", "app", FLATPAK_ID)
    lista.append(("flatpak", os.path.join(flatpak, "config", "nicotine", "config"),
                  os.path.join(flatpak, "data", "nicotine")))
    antiguo = os.path.join(base, ".nicotine")
    if os.path.isdir(antiguo):
        lista.append(("antiguo", os.path.join(antiguo, "config"), antiguo))
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(base, ".config")
    xdg_datos = os.environ.get("XDG_DATA_HOME") or os.path.join(base, ".local", "share")
    lista.append(("mac" if ES_MAC else "nativo",
                  os.path.join(xdg_config, "nicotine", "config"),
                  os.path.join(xdg_datos, "nicotine")))
    return lista


def programa_nicotine_instalado(sabor):
    """Si el Nicotine+ de ese sabor está instalado (sin haberse ejecutado nunca)."""
    import shutil
    if sabor == "snap":
        return os.path.isdir("/snap/nicotine-plus/current")
    if sabor == "flatpak":
        return any(os.path.isdir(os.path.join(b, "app", FLATPAK_ID))
                   for b in ("/var/lib/flatpak",
                             os.path.join(casa(), ".local", "share", "flatpak")))
    if sabor == "mac":
        return os.path.isdir("/Applications/Nicotine+.app") or bool(shutil.which("nicotine"))
    if sabor == "windows":
        return ejecutable_nicotine_windows() is not None
    return bool(shutil.which("nicotine") or shutil.which("nicotine-plus"))


def ejecutable_nicotine_windows():
    import shutil
    rutas = [shutil.which("Nicotine+")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            rutas.append(os.path.join(base, "Nicotine+", "Nicotine+.exe"))
    return next((r for r in rutas if r and os.path.isfile(r)), None)


def _rutas_nicotine(sabor, archivo_config, dir_datos):
    incompletos = os.path.join(dir_datos, "incomplete")
    try:
        with open(archivo_config, encoding="utf-8") as f:
            for linea in f:
                m = re.match(r"^incompletedir\s*=\s*(.+?)\s*$", linea)
                if m:
                    incompletos = m.group(1).replace("${NICOTINE_DATA_HOME}", dir_datos)
                    break
    except OSError:
        pass
    return {
        "sabor": sabor,
        "config": archivo_config,
        "datos": dir_datos,
        "plugins": os.path.join(dir_datos, "plugins"),
        "incompletos": os.path.normpath(incompletos),
    }


def detectar_nicotine():
    """El Nicotine+ de esta máquina.

    Si hay varios (snap y flatpak, por ejemplo), gana el que tenga config y la
    haya escrito más recientemente. Si ninguno se ha ejecutado aún, el primero
    cuyo programa esté instalado; y si tampoco, el nativo.
    """
    candidatos = _candidatos_nicotine()
    con_config = []
    for sabor, archivo, datos in candidatos:
        if os.path.isfile(archivo):
            con_config.append((os.path.getmtime(archivo), sabor, archivo, datos))
    if con_config:
        _, sabor, archivo, datos = max(con_config)
    else:
        sabor, archivo, datos = next(
            (c for c in candidatos if programa_nicotine_instalado(c[0])), candidatos[-1])
    return _rutas_nicotine(sabor, archivo, datos)


_fijado = CONFIG.get("nicotine") or {}
if _fijado.get("config") and _fijado.get("datos"):
    NICOTINE = _rutas_nicotine(_fijado.get("sabor") or "nativo",
                               _fijado["config"], _fijado["datos"])
else:
    NICOTINE = detectar_nicotine()

SABOR_NICOTINE = NICOTINE["sabor"]
CONFIG_NICOTINE = NICOTINE["config"]
DIR_PLUGINS_NICOTINE = NICOTINE["plugins"]
ENLACE_PLUGIN = os.path.join(DIR_PLUGINS_NICOTINE, "flac_hunter")
# Donde Nicotine+ escribe cada archivo mientras lo baja (config: incompletedir).
DIR_INCOMPLETOS = NICOTINE["incompletos"]


# ─── El proceso de Nicotine+ ──────────────────────────────────────────────────

def _argv_es_nicotine(argv):
    """No basta con buscar "nicotine" en la línea de comandos: `snap run --shell
    nicotine-plus -c ...` también la contiene y daba falsos positivos. El
    proceso de verdad ejecuta el script `bin/nicotine` (o Nicotine+.app en
    macOS), así que exigimos un argumento con ruta cuyo nombre base sea
    exactamente uno de NOMBRES_NICOTINE."""
    if any(a in ("--shell", "-c") for a in argv):
        return False
    for arg in argv:
        if "/" not in arg:
            continue
        base = os.path.basename(arg).lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base in NOMBRES_NICOTINE:
            return True
    return False


def _pid_nicotine_proc():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if _argv_es_nicotine(argv):
            return int(pid)
    return None


def _pid_nicotine_ps():
    import subprocess
    try:
        salida = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True,
                                text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for linea in salida.splitlines():
        partes = linea.split()
        if len(partes) >= 2 and partes[0].isdigit() and _argv_es_nicotine(partes[1:]):
            return int(partes[0])
    return None


def _pid_nicotine_tasklist():
    import csv
    import io
    import subprocess
    try:
        salida = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                timeout=15, creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for fila in csv.reader(io.StringIO(salida.decode("utf-8", "replace"))):
        if len(fila) < 2 or not fila[1].isdigit():
            continue
        nombre = fila[0].lower()
        if nombre.endswith(".exe"):
            nombre = nombre[:-4]
        if nombre in NOMBRES_NICOTINE:
            return int(fila[1])
    return None


def pid_nicotine():
    """PID del Nicotine+ real, o None."""
    if ES_WINDOWS:
        return _pid_nicotine_tasklist()
    if os.path.isdir("/proc"):
        return _pid_nicotine_proc()
    return _pid_nicotine_ps()


def arranque_proceso(pid):
    """Marca de tiempo (epoch) en que arrancó el proceso, o None si no se sabe."""
    import subprocess
    try:
        if ES_WINDOWS:
            orden = ["powershell", "-NoProfile", "-Command",
                     f"(Get-Process -Id {pid}).StartTime.ToUniversalTime()"
                     ".ToString('yyyy-MM-ddTHH:mm:ss')"]
            texto = subprocess.run(orden, capture_output=True, text=True, timeout=20,
                                   creationflags=0x08000000).stdout.strip()
            import calendar
            return calendar.timegm(time.strptime(texto, "%Y-%m-%dT%H:%M:%S"))
        if os.path.isdir("/proc"):
            return os.path.getmtime(f"/proc/{pid}")
        texto = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                               text=True, timeout=10,
                               env=dict(os.environ, LC_ALL="C")).stdout.strip()
        return time.mktime(time.strptime(texto, "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def lanzar_nicotine(headless=False):
    """Abre Nicotine+ en segundo plano. Devuelve (True, orden) o (False, motivo)."""
    import shutil
    import subprocess
    orden = None
    if ES_WINDOWS:
        exe = ejecutable_nicotine_windows()
        if exe:
            orden = [exe]
    elif ES_MAC and os.path.isdir("/Applications/Nicotine+.app"):
        orden = ["open", "-a", "Nicotine+"] + (["--args", "--headless"] if headless else [])
        headless = False
    elif SABOR_NICOTINE == "flatpak" and shutil.which("flatpak"):
        orden = ["flatpak", "run", FLATPAK_ID]
    else:
        exe = shutil.which("nicotine-plus") or shutil.which("nicotine")
        if exe:
            orden = [exe]
    if not orden:
        return False, "no encuentro el ejecutable de Nicotine+"
    if headless:
        orden.append("--headless")
    extras = {}
    if ES_WINDOWS:
        extras["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        extras["start_new_session"] = True
    try:
        subprocess.Popen(orden, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **extras)
    except OSError as error:
        return False, str(error)
    return True, " ".join(orden)


# ─── Instalación ──────────────────────────────────────────────────────────────
# Lo usan tanto `flacpipe revisar` como la vista de salud de la interfaz.

def ultimo_arranque_plugin():
    """Marca de tiempo del último '[plugin] activado' en pipeline.log."""
    try:
        with open(ARCHIVO_LOG, encoding="utf-8", errors="replace") as f:
            lineas = [l for l in f if "[plugin] activado" in l]
    except OSError:
        return None
    if not lineas:
        return None
    try:
        return datetime.fromisoformat(lineas[-1].split("  ")[0]).timestamp()
    except ValueError:
        return None


def snap_lee_plugin():
    """Comprueba de verdad que el snap confinado alcanza el código del plugin.
    Sólo tiene sentido con el snap; en cualquier otro caso devuelve None."""
    if SABOR_NICOTINE != "snap":
        return None
    import subprocess
    orden = ("python3 -c \"import os,sys;"
             "sys.exit(0 if os.path.isfile('%s/__init__.py') else 1)\"" % ENLACE_PLUGIN)
    try:
        r = subprocess.run(["snap", "run", "--shell", "nicotine-plus", "-c", orden],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def orden_instalador():
    """Cómo se vuelve a lanzar el instalador en este sistema."""
    return "instalar.bat" if ES_WINDOWS else "bash instalar.sh"
