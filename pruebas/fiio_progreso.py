#!/usr/bin/env python3
"""Comprueba el modelo de la copia al FiiO sin pantalla.

Le pasa a un Trabajo una tanda de eventos como los que documenta
fiio.copiar() (tres álbumes: uno se copia, otro se omite y el último falla a
medias) y mira el modelo: estados, pistas hechas, bytes, inicio y fin, y que
el tiempo restante no se invente antes de tener un par de segundos medidos.
Después, con App sin curses, que la última copia sobrevive a expulsar y que
la velocidad medida queda guardada por tarjeta.

    python3 pruebas/fiio_progreso.py
"""
import os
import sys
import tempfile
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)

# La biblioteca de mentira tiene que estar puesta antes de importar fh_comun:
# así .pipeline (donde se guarda la velocidad) cae en el directorio temporal.
TEMPORAL = tempfile.mkdtemp(prefix="fiio-progreso-")
os.environ["FLACPIPE_BIBLIOTECA"] = TEMPORAL
sys.path.insert(0, RAIZ)
import flacpipe_tui as tui  # noqa: E402

FALLOS = []


def comprobar(cond, texto):
    print(("  ok   " if cond else "  MAL  ") + texto)
    if not cond:
        FALLOS.append(texto)


PLAN = [
    {"rel": "CASIOPEA/Mint Jams", "artista": "CASIOPEA", "album": "Mint Jams",
     "pistas": ["01 Take Me.flac", "02 Asayake.flac"], "bytes": 3_000},
    {"rel": "Nujabes/Modal Soul", "artista": "Nujabes", "album": "Modal Soul",
     "pistas": ["01 Feather.flac", "02 Ordinary Joe.flac"], "bytes": 5_000},
    {"rel": "Tom Misch/Geography", "artista": "Tom Misch", "album": "Geography",
     "pistas": ["01 Before Paris.flac", "02 Lost in Paris.flac", "03 Movie.flac"],
     "bytes": 6_000},
]

EVENTOS = [
    {"t": "album", "rel": "CASIOPEA/Mint Jams", "pistas": 2, "bytes": 3_000},
    {"t": "pista", "rel": "CASIOPEA/Mint Jams", "archivo": "01 Take Me.flac",
     "indice": 1, "total": 2, "bytes": 1_000},
    {"t": "bytes", "n": 1_000},
    {"t": "pista_ok", "archivo": "01 Take Me.flac"},
    {"t": "pista", "rel": "CASIOPEA/Mint Jams", "archivo": "02 Asayake.flac",
     "indice": 2, "total": 2, "bytes": 2_000},
    {"t": "bytes", "n": 2_000},
    {"t": "pista_ok", "archivo": "02 Asayake.flac"},
    {"t": "orden", "rel": "CASIOPEA/Mint Jams", "ok": True},
    {"t": "album_ok", "rel": "CASIOPEA/Mint Jams"},
    {"t": "omitido", "rel": "Nujabes/Modal Soul", "motivo": "ya está y en orden"},
    {"t": "album", "rel": "Tom Misch/Geography", "pistas": 3, "bytes": 6_000},
    {"t": "pista", "rel": "Tom Misch/Geography", "archivo": "01 Before Paris.flac",
     "indice": 1, "total": 3, "bytes": 2_000},
    {"t": "bytes", "n": 2_000},
    {"t": "pista_ok", "archivo": "01 Before Paris.flac"},
    {"t": "pista", "rel": "Tom Misch/Geography", "archivo": "02 Lost in Paris.flac",
     "indice": 2, "total": 3, "bytes": 2_000},
    {"t": "bytes", "n": 500},
    {"t": "error", "rel": "Tom Misch/Geography", "error": "No space left on device"},
]


def modelo_con_eventos():
    print("eventos de una copia: copiado, omitido, error")
    trabajo = tui.Trabajo()
    antes = time.time()
    copia = tui.Trabajo.Copia(PLAN, etiqueta="SNOWSKY", montaje="/media/SNOWSKY")

    comprobar([a["estado"] for a in copia.albumes] == ["espera"] * 3,
              "al principio los tres álbumes esperan")
    comprobar(copia.restante() is None and copia.termina() is None,
              "sin muestras no hay tiempo restante ni hora de fin")
    comprobar(copia.total_bytes == 14_000 and copia.frac() == 0.0,
              "el total sale del plan y el avance es 0")
    comprobar(copia.fin is None and not copia.terminada, "sin fin mientras corre")

    vistos = {}

    def funcion(t):
        for ev in EVENTOS:
            t.evento(ev)
            if ev["t"] == "pista" and ev["indice"] == 1 and "a" not in vistos:
                vistos["a"] = (copia.album_indice, copia.actual()["estado"],
                               copia.pista_indice)
        vistos["restante_al_final"] = copia.restante()
        return {"copiados": 1, "omitidos": 1, "archivos": 3, "bytes": 5_000,
                "desordenados": [], "errores": ["Tom Misch/Geography: No space"],
                "abortado": False}

    trabajo.lanzar("copiando al FiiO · 3 álbumes", funcion, copia=copia)
    trabajo.hilo.join(5)

    comprobar(trabajo.copia is copia, "el trabajo lleva el modelo de la copia")
    comprobar(vistos.get("a") == (0, "copiando", 1),
              "al empezar el primer álbum: índice 0, copiando, pista 1")
    estados = [a["estado"] for a in copia.albumes]
    comprobar(estados == ["copiado", "omitido", "error"],
              f"estados finales copiado/omitido/error ({estados})")
    comprobar([a["hechas"] for a in copia.albumes] == [2, 0, 1],
              "pistas hechas por álbum: 2, 0, 1")
    comprobar(copia.albumes[0]["orden_ok"] is True, "orden verificado en el primero")
    comprobar(copia.albumes[1]["motivo"] == "ya está y en orden",
              "el omitido dice por qué")
    comprobar(copia.albumes[2]["motivo"] == "No space left on device",
              "el que falló dice el error")
    comprobar(copia.errores == [{"rel": "Tom Misch/Geography",
                                 "error": "No space left on device"}],
              "la lista de errores tiene el del tercer álbum")
    comprobar(copia.hechos_bytes == 5_500, "bytes escritos: 5 500")
    comprobar(copia.bytes_pendientes == 0 and copia.total_bytes == 5_500,
              "sin nada pendiente, el total es lo escrito (omitido y error fuera)")
    comprobar(copia.contar("copiado") == 1 and copia.contar("omitido") == 1
              and copia.contar("error") == 1, "contar() por estado")
    comprobar(copia.pistas_total == 5 and copia.pistas_hechas == 3,
              "pistas: 3 de 5 (el omitido no cuenta)")
    comprobar(copia.album_indice == 2 and copia.pista_indice == 2,
              "en curso al final: tercer álbum, pista 2")
    comprobar(vistos.get("restante_al_final") is None,
              "restante None sin un par de segundos de muestras")
    comprobar(copia.terminada and copia.fin is not None
              and antes <= copia.inicio <= copia.fin <= time.time(),
              "inicio y fin son horas de reloj y fin llega al acabar")
    comprobar(copia.resumen is not None and copia.resumen["copiados"] == 1,
              "guarda el resumen de fiio.copiar()")
    comprobar(copia.velocidad_media() is None,
              "una copia de menos de 2 s no da velocidad media")
    comprobar(trabajo.album == "Tom Misch/Geography" and trabajo.hechos_bytes == 5_500,
              "los campos de siempre siguen moviéndose (panel_trabajo)")


def modelo_abortado():
    print("copia parada a medias")
    trabajo = tui.Trabajo()
    copia = tui.Trabajo.Copia(PLAN[:2])

    def funcion(t):
        t.evento({"t": "album", "rel": "CASIOPEA/Mint Jams", "pistas": 2, "bytes": 3_000})
        t.evento({"t": "pista", "rel": "CASIOPEA/Mint Jams", "archivo": "01 Take Me.flac",
                  "indice": 1, "total": 2, "bytes": 1_000})
        t.evento({"t": "bytes", "n": 1_000})
        t.evento({"t": "pista_ok", "archivo": "01 Take Me.flac"})
        t.cancelar.set()
        t.evento({"t": "abortado", "rel": "CASIOPEA/Mint Jams"})
        return {"copiados": 0, "abortado": True, "errores": []}

    trabajo.lanzar("copiando", funcion, copia=copia)
    trabajo.hilo.join(5)
    comprobar([a["estado"] for a in copia.albumes] == ["abortado", "espera"],
              "el álbum a medias queda abortado y el siguiente en espera")
    comprobar(copia.abortado and "1 de 2" in copia.albumes[0]["motivo"],
              "dice tras cuántas pistas se paró")

    # Una copia que acaba sin el evento (la de mentira de conductor.py)
    trabajo = tui.Trabajo()
    copia = tui.Trabajo.Copia(PLAN[:1])

    def corta(t):
        t.evento({"t": "album", "rel": "CASIOPEA/Mint Jams", "pistas": 2, "bytes": 3_000})
        t.cancelar.set()
        return {"copiados": 0}

    trabajo.lanzar("copiando", corta, copia=copia)
    trabajo.hilo.join(5)
    comprobar(copia.albumes[0]["estado"] == "abortado",
              "cancelada sin evento: el álbum en curso queda abortado al cerrar")


def modelo_restante():
    print("tiempo restante con la velocidad medida")
    copia = tui.Trabajo.Copia([{"rel": "A/B", "artista": "A", "album": "B",
                                "pistas": ["01.flac"], "bytes": 100_000_000}],
                              etiqueta="SNOWSKY")
    copia.evento({"t": "album", "rel": "A/B", "pistas": 1, "bytes": 100_000_000})
    copia.evento({"t": "pista", "rel": "A/B", "archivo": "01.flac", "indice": 1,
                  "total": 1, "bytes": 100_000_000})
    fin = time.monotonic() + 2.6
    escrito = 0
    while time.monotonic() < fin:
        time.sleep(0.1)
        copia.evento({"t": "bytes", "n": 1_000_000})
        escrito += 1_000_000
    restante = copia.restante()
    velocidad = copia.velocidad()
    comprobar(velocidad is not None and 3e6 < velocidad < 2e7,
              f"velocidad medida razonable ({velocidad and int(velocidad)} B/s)")
    comprobar(restante is not None and restante > 0,
              f"con ~2,5 s de muestras ya hay restante ({restante and round(restante)} s)")
    comprobar(restante is not None
              and abs(restante - (100_000_000 - escrito) / velocidad) < 1e-6,
              "restante = bytes pendientes / velocidad media")
    termina = copia.termina()
    comprobar(termina is not None and termina > time.time(), "termina ~HH:MM en el futuro")

    simulada = tui.Trabajo.Copia(PLAN[:1], simulacion=True)
    for ev in EVENTOS[:7]:
        simulada.evento(ev)
    comprobar(simulada.restante() is None and simulada.velocidad_media() is None,
              "una simulación no promete tiempos")
    comprobar(simulada.frac() == 1.0, "una simulación avanza por pistas")


class Pantalla:
    """Lo único que App toca de curses al construirse."""
    def getmaxyx(self):
        return (40, 120)


def app_ultima_copia():
    print("App: última copia, expulsar y velocidad por tarjeta")
    llamadas = []

    def copiar(destino_raiz, albumes, simular=False, rehacer=False, informar=print,
               evento=None, cancelado=None):
        llamadas.append(destino_raiz)
        for a in albumes:
            evento({"t": "album", "rel": a["rel"], "pistas": len(a["pistas"]),
                    "bytes": a["bytes"]})
            for i, p in enumerate(a["pistas"], 1):
                por_pista = a["bytes"] // len(a["pistas"])
                evento({"t": "pista", "rel": a["rel"], "archivo": p, "indice": i,
                        "total": len(a["pistas"]), "bytes": por_pista})
                for _ in range(6):
                    time.sleep(0.1)
                    evento({"t": "bytes", "n": por_pista // 6})
                evento({"t": "pista_ok", "archivo": p})
            evento({"t": "album_ok", "rel": a["rel"]})
        return {"copiados": len(albumes), "archivos": 0, "bytes": 0, "errores": []}

    expulsiones = []

    def desmontar(montaje, informar=print):
        expulsiones.append(montaje)
        informar("hecho")
        return True

    tui.fiio.copiar = copiar
    tui.fiio.desmontar = desmontar
    app = tui.App(Pantalla())
    comprobar(app.ultima_copia is None and app.velocidades_fiio == {},
              "sin copias: nada guardado")
    destino = {"montaje": os.path.join(TEMPORAL, "tarjeta"), "etiqueta": "SNOWSKY",
               "tipo": "vfat", "libre": 10**10, "total": 10**10}
    app.copiar_fiio(destino, PLAN[:2])
    copia = app.trabajo.copia
    comprobar(copia is not None and copia.etiqueta == "SNOWSKY"
              and len(copia.albumes) == 2, "copiar_fiio llena el modelo antes de lanzar")
    app.trabajo.hilo.join(10)
    comprobar(app.ultima_copia is copia and copia.terminada,
              "al acabar el hilo la copia queda en app.ultima_copia")
    media = app.velocidades_fiio.get("SNOWSKY")
    comprobar(media is not None and media > 0, "la velocidad medida queda por tarjeta")
    comprobar(tui.Trabajo.Copia.leer_velocidades().get("SNOWSKY") == media,
              "y se guarda en .pipeline para la próxima vez")

    app.expulsar(destino)
    for _ in range(50):
        if not app.expulsando:
            break
        time.sleep(0.05)
    comprobar(expulsiones == [destino["montaje"]], "expulsar llama a fiio.desmontar")
    comprobar(app.ultima_copia is copia
              and [a["estado"] for a in copia.albumes] == ["copiado", "copiado"],
              "expulsar no borra la última copia")
    comprobar(copia.expulsada is True, "y anota que la tarjeta quedó expulsada")
    comprobar(app.ultima_expulsion == {"montaje": destino["montaje"],
                                       "etiqueta": "SNOWSKY", "ok": True},
              "ultima_expulsion dice qué tarjeta y si salió bien")
    comprobar(app.trabajo.copia is copia and app.trabajo.clase == "copia",
              "el trabajo de la copia sigue intacto tras expulsar")


if __name__ == "__main__":
    try:
        modelo_con_eventos()
        modelo_abortado()
        modelo_restante()
        app_ultima_copia()
    finally:
        import shutil
        shutil.rmtree(TEMPORAL, ignore_errors=True)
    if FALLOS:
        print(f"FALLARON {len(FALLOS)}:", *FALLOS, sep="\n  ")
        sys.exit(1)
    print("todo bien")
