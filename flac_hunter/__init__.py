"""FLAC Hunter — parte 1 del pipeline: buscar, elegir y descargar FLAC en orden.

A diferencia de la versión anterior, este plugin NO usa la wishlist de
Nicotine+ (que sólo lanza un término cada ~12 minutos y no da control del
orden). En su lugar mantiene su propia cola en

    ~/Música/.pipeline/cola.json

y procesa **una petición a la vez**: busca, espera resultados, puntúa las
carpetas candidatas, descarga la mejor a una carpeta temporal aislada y, al
terminar, la organiza en ~/Música/Artista/Álbum/NN Título.flac.

La cola se alimenta desde fuera con la CLI `flacpipe pedir "..."`, o desde
dentro de Nicotine+ con el comando /flac.
"""

import os
import time
import traceback

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin
from pynicotine.transfers import TransferStatus

import fh_biblioteca as biblioteca
import fh_comun as comun

# Claves de FileAttribute (pynicotine/slskmessages.py). Los atributos que
# manda un peer son un dict con claves enteras, no un objeto.
ATTR_SAMPLE_RATE = 4
ATTR_BIT_DEPTH = 5

# Puntos por profundidad de bits. 24 es el óptimo real: 32-bit en Soulseek casi
# siempre es un 24-bit re-empaquetado, así que puntúa algo por debajo.
TABLA_BITS = {8: -80, 16: 40, 20: 90, 24: 130, 32: 115}

ESTADOS_FALLIDOS = {
    TransferStatus.CANCELLED,
    TransferStatus.FILTERED,
    TransferStatus.USER_LOGGED_OFF,
    TransferStatus.CONNECTION_CLOSED,
    TransferStatus.CONNECTION_TIMEOUT,
    TransferStatus.DOWNLOAD_FOLDER_ERROR,
    TransferStatus.LOCAL_FILE_ERROR,
}

PALABRAS_SOSPECHOSAS = ("discograph", "complete works", "anthology", "box set", "boxset")


class Plugin(BasePlugin):

    settings = {
        "pausado": False,
        "bits_minimos": 16,
        "bits_maximos": 32,
        "ventana_busqueda": 45,
        "min_pistas_album": 4,
        "max_intentos": 3,
        "timeout_sin_avance": 600,
        "timeout_descarga": 14400,
        "añadir_flac_a_consulta": True,
        "verboso": True,
    }

    metasettings = {
        "pausado": {
            "description": "Pausar el pipeline (no toma peticiones nuevas)",
            "type": "bool",
        },
        "bits_minimos": {
            "description": "Profundidad de bits mínima aceptable (16 / 24)",
            "type": "int",
        },
        "bits_maximos": {
            "description": "Profundidad de bits máxima aceptable (FLAC no pasa de 32)",
            "type": "int",
        },
        "ventana_busqueda": {
            "description": "Segundos acumulando resultados antes de elegir",
            "type": "int",
        },
        "min_pistas_album": {
            "description": "Mínimo de pistas FLAC para aceptar una carpeta como álbum",
            "type": "int",
        },
        "max_intentos": {
            "description": "Fuentes distintas a probar antes de dar la petición por fallida",
            "type": "int",
        },
        "timeout_sin_avance": {
            "description": "Segundos sin que avance un solo byte antes de cambiar de fuente",
            "type": "int",
        },
        "timeout_descarga": {
            "description": "Tope absoluto de segundos por álbum (red de seguridad)",
            "type": "int",
        },
        "añadir_flac_a_consulta": {
            "description": "Añadir la palabra 'flac' a la búsqueda si no está",
            "type": "bool",
        },
        "verboso": {
            "description": "Registrar cada candidato encontrado",
            "type": "bool",
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands = {
            "flac": {
                "callback": self.cmd_pedir,
                "description": "Encolar un álbum o canción para descargar en FLAC",
                "parameters": ["<artista - álbum>"],
            },
            "flacestado": {
                "aliases": ["flacs"],
                "callback": self.cmd_estado,
                "description": "Mostrar el estado de la cola del pipeline",
            },
            "flacpausa": {
                "callback": self.cmd_pausa,
                "description": "Pausar o reanudar el pipeline",
            },
        }

    # ─── Ciclo de vida ────────────────────────────────────────────────────────

    def init(self):
        self._pet = None
        self._token = None
        self._cands = {}
        self._t0 = 0.0
        self._archivos = []
        self._destino = None
        self._ocupado = False
        self._ultimo_progreso = 0.0
        self._ultimo_avance = 0.0
        self._bytes_previos = 0

        comun.asegurar_dirs()
        self._recuperar_cola()

        events.connect("file-search-response", self._respuesta_busqueda)
        self._timer = events.schedule(delay=3, callback=self._tick, repeat=True)

        self.log("Pipeline listo. Cola: %s", msg_args=(comun.ARCHIVO_COLA,))
        comun.registrar("[plugin] activado")

    def disable(self):
        try:
            events.cancel_scheduled(self._timer)
        except Exception:
            pass
        try:
            events.disconnect("file-search-response", self._respuesta_busqueda)
        except (ValueError, KeyError):
            pass

        self._cerrar_busqueda()
        if self._pet is not None:
            # Devolver la petición a la cola para retomarla al reactivar
            self._actualizar(self._pet, estado=comun.PENDIENTE,
                             mensaje="interrumpida al desactivar el plugin")
            self._pet = None

        comun.registrar("[plugin] desactivado")
        self.log("Pipeline detenido")

    def _recuperar_cola(self):
        """Tras un cierre inesperado, devuelve a 'pendiente' lo que quedó en curso."""
        def _op(cola):
            rescatadas = 0
            for pet in cola["peticiones"]:
                if pet["estado"] in comun.EN_CURSO:
                    pet["estado"] = comun.PENDIENTE
                    pet["actualizado"] = comun.ahora()
                    pet["mensaje"] = "retomada tras reiniciar Nicotine+"
                    rescatadas += 1
            return rescatadas

        rescatadas = comun.modificar_cola(_op)
        if rescatadas:
            self.log("%d petición(es) retomadas de la sesión anterior", msg_args=(rescatadas,))

    # ─── Comandos ─────────────────────────────────────────────────────────────

    def cmd_pedir(self, args, **_kwargs):
        consulta = args.strip()
        if not consulta:
            self.output("Uso: /flac Radiohead - OK Computer")
            return False
        creadas = comun.añadir_peticiones([consulta])
        if not creadas:
            self.output(f"'{consulta}' ya estaba en la cola.")
        else:
            self.output(f"Encolado: {consulta}")
        return True

    def cmd_estado(self, _args, **_kwargs):
        cola = comun.leer_cola()
        peticiones = cola["peticiones"]
        if not peticiones:
            self.output("La cola está vacía.")
            return True
        for pet in peticiones[-15:]:
            linea = f"[{pet['estado']}] {pet['consulta']}"
            if pet.get("progreso"):
                p = pet["progreso"]
                linea += f" — {p.get('terminados', 0)}/{p.get('total', 0)} archivos"
            if pet.get("resultado"):
                linea += f" → {pet['resultado'].get('carpeta')} ({pet['resultado'].get('calidad')})"
            if pet.get("mensaje"):
                linea += f" — {pet['mensaje']}"
            self.output(linea)
        return True

    def cmd_pausa(self, _args, **_kwargs):
        self.settings["pausado"] = not self.settings["pausado"]
        self.output("Pipeline pausado." if self.settings["pausado"] else "Pipeline reanudado.")
        return True

    # ─── Bucle principal ──────────────────────────────────────────────────────

    def _tick(self):
        if self._ocupado:
            return
        self._ocupado = True
        try:
            self._paso()
        except Exception:
            rastro = traceback.format_exc()
            comun.registrar("ERROR en el ciclo:\n" + rastro)
            self.log("Error en el ciclo (ver %s):\n%s",
                     msg_args=(comun.ARCHIVO_LOG, rastro))
            # No dejar una petición atascada por un error puntual
            if self._pet is not None:
                self._fallar(self._pet, "error interno del plugin")
        finally:
            self._ocupado = False

    def _conectado(self):
        return bool(getattr(self.core.users, "login_username", None))

    def _paso(self):
        if self.settings["pausado"] or not self._conectado():
            return
        if self._pet is None:
            self._tomar_siguiente()
            return
        if self._pet["estado"] == comun.BUSCANDO:
            self._fase_busqueda()
        elif self._pet["estado"] == comun.DESCARGANDO:
            self._fase_descarga()
        else:
            self._pet = None

    def _tomar_siguiente(self):
        def _op(cola):
            for pet in cola["peticiones"]:
                if pet["estado"] == comun.PENDIENTE:
                    pet["estado"] = comun.BUSCANDO
                    pet["actualizado"] = comun.ahora()
                    pet["mensaje"] = "buscando fuentes"
                    return dict(pet)
            return None

        pet = comun.modificar_cola(_op)
        if pet is None:
            return
        self._pet = pet
        self._iniciar_busqueda()

    # ─── Fase 1: búsqueda ─────────────────────────────────────────────────────

    def _termino(self):
        consulta = self._pet["consulta"]
        if self.settings["añadir_flac_a_consulta"] and "flac" not in consulta.casefold():
            return f"{consulta} flac"
        return consulta

    def _iniciar_busqueda(self):
        self._cands = {}
        self._archivos = []
        termino = self._termino()
        # switch_page=False para que Nicotine+ no salte de pestaña sola
        self.core.search.do_search(termino, "global", switch_page=False)
        self._token = self.core.search.token
        self._t0 = time.monotonic()
        comun.registrar(f"[buscar] {self._pet['id']} token={self._token} término={termino!r}")
        self.log("Buscando: %s", msg_args=(termino,))

    def _cerrar_busqueda(self):
        if self._token is not None:
            try:
                self.core.search.remove_search(self._token)
            except Exception:
                pass
            self._token = None

    def _respuesta_busqueda(self, msg):
        """Cada respuesta de un peer a nuestra búsqueda activa."""
        if self._token is None or msg.token != self._token:
            return

        usuario = getattr(msg, "username", None) or getattr(msg, "search_username", None)
        if not usuario or not msg.list:
            return

        por_carpeta = {}
        for info in msg.list:
            try:
                nombre = info[1]
            except (TypeError, IndexError):
                continue
            if not nombre.lower().endswith(biblioteca.EXT_AUDIO):
                continue
            carpeta = nombre.rpartition("\\")[0]
            por_carpeta.setdefault(carpeta, []).append(info)

        for carpeta, archivos in por_carpeta.items():
            clave = (usuario, carpeta)
            cand = self._cands.get(clave)
            if cand is None:
                cand = self._cands[clave] = {
                    "usuario": usuario,
                    "carpeta": carpeta,
                    "archivos": [],
                    "rutas": set(),
                    "velocidad": msg.ulspeed or 0,
                    "slots_libres": bool(msg.freeulslots),
                    "en_cola": msg.inqueue or 0,
                }
            for info in archivos:
                if info[1] not in cand["rutas"]:
                    cand["rutas"].add(info[1])
                    cand["archivos"].append(info)

    def _fase_busqueda(self):
        transcurrido = time.monotonic() - self._t0
        ventana = max(10, self.settings["ventana_busqueda"])

        if transcurrido < ventana:
            return
        # Si no llegó nada, damos dos ventanas más de gracia antes de rendirnos
        if not self._cands and transcurrido < ventana * 3:
            return

        self._cerrar_busqueda()
        self._elegir()

    # ─── Puntuación de candidatos ─────────────────────────────────────────────

    def _atributos(self, info):
        attrs = info[4] if len(info) > 4 else None
        if not isinstance(attrs, dict):
            return 0, 0
        return attrs.get(ATTR_BIT_DEPTH) or 0, attrs.get(ATTR_SAMPLE_RATE) or 0

    def _puntuar(self, cand):
        """Devuelve (puntos, detalle). puntos=None significa descartado."""
        archivos = cand["archivos"]
        n = len(archivos)
        if not n:
            return None, "sin archivos FLAC"

        bits, tasas = [], []
        for info in archivos:
            bd, sr = self._atributos(info)
            if bd:
                bits.append(bd)
            if sr:
                tasas.append(sr)

        bits_max = max(bits) if bits else 0
        tasa_max = max(tasas) if tasas else 0

        # ── Filtros duros de calidad ──
        if bits_max and bits_max < self.settings["bits_minimos"]:
            return None, f"{bits_max}-bit por debajo del mínimo"
        if bits_max and bits_max > self.settings["bits_maximos"]:
            return None, f"{bits_max}-bit por encima del máximo"

        es_album = self._pet["tipo"] == "album"
        minimo = self.settings["min_pistas_album"] if es_album else 1
        if n < minimo:
            return None, f"sólo {n} pista(s), se pedían {minimo}"

        puntos = 0

        # Calidad conocida
        if bits:
            puntos += TABLA_BITS.get(bits_max, min(bits_max, 32) * 4)
        else:
            # Sin atributos no sabemos qué es: aceptable, pero nunca por delante
            # de una fuente que sí declara 24-bit.
            puntos -= 60
        if tasa_max:
            puntos += min(int(tasa_max / 4000), 56)

        # Proporción de archivos que declaran calidad
        puntos += int(20 * len(bits) / n)

        # Completitud del álbum
        if es_album:
            puntos += min(n * 3, 45)

        # Salud del peer
        puntos += min(int((cand["velocidad"] / (1024 * 1024)) * 3), 30)
        if cand["slots_libres"]:
            puntos += 25
        puntos -= min(cand["en_cola"] * 2, 30)

        # Coincidencia con lo pedido
        ruta = cand["carpeta"].casefold()
        palabras = [p for p in self._pet["consulta"].casefold().split() if len(p) > 2]
        if palabras:
            aciertos = sum(1 for p in palabras if p in ruta)
            puntos += int(30 * aciertos / len(palabras))

        # Una discografía completa no es lo que se pidió
        if any(p in ruta for p in PALABRAS_SOSPECHOSAS):
            puntos -= 70

        detalle = f"{n} pistas, {bits_max or '?'}-bit"
        if tasa_max:
            detalle += f" / {tasa_max / 1000:.6g} kHz"
        return puntos, detalle

    def _elegir(self):
        pet = self._pet
        rechazados = {tuple(r) for r in pet.get("rechazados", [])}

        puntuados = []
        for clave, cand in self._cands.items():
            if clave in rechazados:
                continue
            puntos, detalle = self._puntuar(cand)
            if puntos is None:
                if self.settings["verboso"]:
                    comun.registrar(f"[descarte] {cand['usuario']}: {detalle}")
                continue
            puntuados.append((puntos, detalle, cand))

        puntuados.sort(key=lambda x: x[0], reverse=True)

        if not puntuados:
            self._fallar(pet, f"sin fuentes válidas entre {len(self._cands)} resultados")
            return

        if self.settings["verboso"]:
            for puntos, detalle, cand in puntuados[:5]:
                comun.registrar(f"[candidato] {puntos:>5}  {cand['usuario']}  {detalle}  "
                                f"{cand['carpeta'].rpartition(chr(92))[2]}")

        puntos, detalle, mejor = puntuados[0]
        self.log("Elegido: %s — %s (%d pts)", msg_args=(mejor["usuario"], detalle, puntos))
        self._encolar(mejor, puntos, detalle)

    # ─── Fase 2: descarga ─────────────────────────────────────────────────────

    def _encolar(self, cand, puntos, detalle):
        pet = self._pet
        destino = os.path.join(comun.DIR_DESCARGAS, pet["id"])
        os.makedirs(destino, exist_ok=True)
        self._destino = destino
        self._archivos = []

        for info in cand["archivos"]:
            nombre = info[1]
            tamaño = info[2] if len(info) > 2 else 0
            attrs = info[4] if len(info) > 4 and isinstance(info[4], dict) else None
            self.core.downloads.enqueue_download(
                username=cand["usuario"],
                virtual_path=nombre,
                folder_path=destino,
                size=tamaño,
                file_attributes=attrs,
                bypass_filter=True,
            )
            self._archivos.append((cand["usuario"], nombre))

        self._t0 = time.monotonic()
        self._ultimo_progreso = 0.0
        self._ultimo_avance = time.monotonic()
        self._bytes_previos = 0
        comun.registrar(f"[descargar] {pet['id']} {len(self._archivos)} archivos "
                        f"de {cand['usuario']} → {destino}")
        self._actualizar(
            pet,
            estado=comun.DESCARGANDO,
            mensaje=f"descargando de {cand['usuario']}",
            candidato={
                "usuario": cand["usuario"],
                "carpeta": cand["carpeta"],
                "puntos": puntos,
                "detalle": detalle,
            },
            progreso={"terminados": 0, "total": len(self._archivos)},
        )

    def _transferencias(self):
        registro = self.core.downloads.transfers
        for usuario, ruta in self._archivos:
            t = registro.get(usuario + ruta)
            if t is not None:
                yield t

    def _fase_descarga(self):
        pet = self._pet
        terminados = fallidos = activos = 0
        bytes_totales = 0

        for t in self._transferencias():
            bytes_totales += getattr(t, "current_byte_offset", 0) or 0
            if t.status == TransferStatus.FINISHED:
                terminados += 1
            elif t.status in ESTADOS_FALLIDOS:
                fallidos += 1
            else:
                activos += 1

        perdidos = len(self._archivos) - (terminados + fallidos + activos)
        fallidos += max(perdidos, 0)

        ahora_mono = time.monotonic()

        # Lo que importa no es cuánto lleva, sino si sigue avanzando: un álbum
        # grande desde un peer lento es normal; uno que no mueve un byte, no.
        if bytes_totales > self._bytes_previos:
            self._bytes_previos = bytes_totales
            self._ultimo_avance = ahora_mono

        if ahora_mono - self._ultimo_progreso > 20:
            self._ultimo_progreso = ahora_mono
            self._actualizar(pet, progreso={"terminados": terminados,
                                            "total": len(self._archivos),
                                            "bytes": bytes_totales})

        if activos == 0:
            if terminados == 0:
                self._reintentar(pet, "la fuente no entregó ningún archivo")
            else:
                self._cerrar_descarga(terminados, fallidos)
            return

        estancada = ahora_mono - self._ultimo_avance > self.settings["timeout_sin_avance"]
        pasada_de_tiempo = ahora_mono - self._t0 > self.settings["timeout_descarga"]
        if estancada or pasada_de_tiempo:
            motivo = ("sin avanzar un byte en "
                      f"{int((ahora_mono - self._ultimo_avance) / 60)} min"
                      if estancada else "se agotó el tope absoluto de tiempo")
            self._abortar_pendientes()
            self._cerrar_descarga(terminados, len(self._archivos) - terminados,
                                  motivo=motivo)

    def _cerrar_descarga(self, terminados, sin_descargar, motivo=None):
        """Decide si el álbum está lo bastante completo o hay que cambiar de fuente."""
        pet = self._pet
        completo = sin_descargar == 0

        if not completo and pet.get("intentos", 0) + 1 < self.settings["max_intentos"]:
            # Un álbum al que le falta una pista no sirve: probamos otra fuente
            # entera antes de conformarnos.
            self._reintentar(pet, motivo or f"faltaron {sin_descargar} archivo(s)")
            return

        if motivo:
            comun.registrar(f"[descargar] {pet['id']} {motivo}; "
                            f"{sin_descargar} archivo(s) sin descargar")
        self._organizar(terminados, sin_descargar, parcial=not completo)

    def _abortar_pendientes(self):
        pendientes = [t for t in self._transferencias()
                      if t.status != TransferStatus.FINISHED]
        if pendientes:
            try:
                self.core.downloads.abort_downloads(pendientes)
            except Exception:
                pass

    def _limpiar_transferencias(self):
        transferencias = list(self._transferencias())
        if transferencias:
            try:
                self.core.downloads.clear_downloads(downloads=transferencias)
            except Exception:
                pass

    # ─── Fase 3: organizar ────────────────────────────────────────────────────

    def _organizar(self, terminados, fallidos, parcial=False):
        pet = self._pet
        self._limpiar_transferencias()

        resultado = biblioteca.organizar(
            self._destino,
            artista_sug=pet.get("artista"),
            album_sug=pet.get("album"),
        )

        if not resultado.get("ok"):
            self._reintentar(pet, resultado.get("motivo", "no se pudo organizar"))
            return

        for aviso in resultado.get("avisos", []):
            comun.registrar(f"[organizar] {pet['id']} {aviso}")

        faltantes = resultado.get("faltantes") or []
        mensaje = f"{resultado['pistas']} pistas en {resultado['carpeta']}"
        if faltantes:
            mensaje += " — INCOMPLETO, faltan las pistas " + ", ".join(
                f"{n:02d}" for n in faltantes)
        elif parcial:
            mensaje += f" (parcial: {fallidos} archivo(s) sin descargar)"

        comun.registrar(f"[listo] {pet['id']} → {resultado['carpeta']} "
                        f"({resultado['pistas']} pistas, {resultado['calidad']})")
        self.log("Listo: %s — %s", msg_args=(resultado["carpeta"], resultado["calidad"]))

        self._actualizar(
            pet,
            estado=comun.LISTO,
            mensaje=mensaje,
            resultado={
                "carpeta": resultado["carpeta"],
                "artista": resultado["artista"],
                "album": resultado["album"],
                "pistas": resultado["pistas"],
                "calidad": resultado["calidad"],
                "parcial": parcial,
                "faltantes": faltantes,
            },
            progreso={"terminados": terminados, "total": len(self._archivos)},
        )
        self._terminar()

    # ─── Reintentos y fallos ──────────────────────────────────────────────────

    def _reintentar(self, pet, motivo):
        self._abortar_pendientes()
        self._limpiar_transferencias()

        cand = pet.get("candidato")
        rechazados = list(pet.get("rechazados", []))
        if cand:
            rechazados.append([cand["usuario"], cand["carpeta"]])

        intentos = pet.get("intentos", 0) + 1
        comun.registrar(f"[reintento {intentos}] {pet['id']}: {motivo}")

        if intentos >= self.settings["max_intentos"]:
            self._fallar(pet, f"{motivo} (tras {intentos} intentos)")
            return

        # Descartamos la carpeta temporal a medio bajar
        if self._destino and os.path.isdir(self._destino):
            import shutil
            shutil.rmtree(self._destino, ignore_errors=True)

        self._actualizar(pet, estado=comun.PENDIENTE, intentos=intentos,
                         rechazados=rechazados, candidato=None, progreso=None,
                         mensaje=f"reintentando: {motivo}")
        self._terminar()

    def _fallar(self, pet, motivo):
        self._abortar_pendientes()
        self._limpiar_transferencias()
        self._cerrar_busqueda()
        comun.registrar(f"[fallo] {pet['id']}: {motivo}")
        self.log("Falló '%s': %s", msg_args=(pet["consulta"], motivo))
        self._actualizar(pet, estado=comun.FALLIDO, mensaje=motivo)
        self._terminar()

    def _terminar(self):
        self._pet = None
        self._cands = {}
        self._archivos = []
        self._destino = None
        self._cerrar_busqueda()

    # ─── Persistencia ─────────────────────────────────────────────────────────

    def _actualizar(self, pet, **campos):
        pet.update(campos)
        pet["actualizado"] = comun.ahora()

        def _op(cola):
            guardada = comun.buscar_peticion(cola, pet["id"])
            if guardada is not None:
                guardada.update(pet)

        comun.modificar_cola(_op)

    # ─── Notificaciones de Nicotine+ ──────────────────────────────────────────

    def download_finished_notification(self, user, virtual_path, real_path):
        if self._pet is None or not self.settings["verboso"]:
            return
        if (user, virtual_path) in self._archivos:
            comun.registrar(f"[archivo] {os.path.basename(real_path)}")
