# flacpipe

Pipeline para llenar un DAP **FiiO Snowsky** con FLAC de alta resolución bajados
de Soulseek, organizados y —lo importante— **en orden**.

Son dos partes independientes. La primera es automática y vive dentro de
Nicotine+. La segunda la disparas tú cuando conectas la microSD.

```
              flacpipe pedir "Artista - Álbum"
                            │
                            ▼
            ~/Música/.pipeline/cola.json          una petición a la vez
                            │
      ┌─────────────────────┴──────────────────────┐
      │   plugin flac_hunter, dentro de Nicotine+   │
      │   buscar → puntuar → descargar → organizar  │
      └─────────────────────┬──────────────────────┘
                            ▼
      ~/Música/Artista/Álbum/01 Canción.flac        ← fin de la PARTE 1
                            │
                    flacpipe fiio                   ← PARTE 2, manual
                            ▼
      /run/media/$USER/<TARJETA>/Artistas/…
```

## El problema que resuelve

El FiiO Snowsky **no ordena por metadatos**. Muestra los archivos en el orden en
que aparecen en la tabla de directorios de la tarjeta, que es el orden en que se
crearon las entradas. Si copias un álbum arrastrándolo desde el escritorio, el
gestor de archivos copia varias pistas en paralelo y quedan desordenadas para
siempre — y no hay forma de arreglarlo desde el reproductor.

De ahí las dos reglas que gobiernan todo el proyecto:

1. Los archivos se llaman `01 Título.flac`, con el número de pista delante y cero
   a la izquierda hasta el 09, para que el orden alfabético coincida con el de
   reproducción.
2. La copia a la tarjeta es **estrictamente secuencial**: un archivo a la vez,
   artista → álbum → pista 01, 02, 03…

## Requisitos

- Linux con Nicotine+ 3.3.x (probado con el snap 3.3.10) y una cuenta de Soulseek.
- Python 3.8+. **Sin dependencias externas**: los tags Vorbis y el bloque
  STREAMINFO se leen a mano, porque el plugin corre dentro del snap y ahí no hay pip.

## Instalación

```bash
git clone git@github.com:yahel-cc/flacpipe.git
cd flacpipe
bash instalar.sh      # con Nicotine+ CERRADO
```

El instalador:

- enlaza `flac_hunter/` en la carpeta de plugins del snap (**enlace simbólico**:
  editas el código aquí y basta reiniciar Nicotine+, no hay que reinstalar);
- añade `flac_hunter` a los plugins habilitados de Nicotine+;
- apunta `downloaddir` a `~/Música/.descargas`, para que una descarga manual no
  ensucie la raíz de la biblioteca;
- enlaza `flacpipe` en `~/.local/bin`.

Las rutas están fijas en `flac_hunter/fh_comun.py` (`RAIZ_BIBLIOTECA`) y en
`instalar.sh`. Si tu usuario no es `yahel` o tu música no está en `~/Música`,
cámbialas ahí.

## Comprobar que funciona

```bash
flacpipe revisar            # instalación, plugin, cola, álbumes incompletos, tarjeta
flacpipe revisar --prueba   # además organiza un álbum falso, sin tocar la red
```

`revisar` sabe si el plugin **se cargó de verdad** en esta sesión de Nicotine+:
el plugin escribe `[plugin] activado` en `pipeline.log` al arrancar, y el comando
compara esa marca con la hora de arranque del proceso. Si Nicotine+ está abierto
y la línea no aparece, el plugin reventó al cargar y su log lo dirá.

## Parte 1 — pedir y descargar

Nicotine+ tiene que estar abierto y conectado.

```bash
flacpipe pedir "Radiohead - OK Computer"
flacpipe pedir "Nujabes - Modal Soul" "Mk.gee - Two Star & The Dream Police"
flacpipe pedir --archivo albums.txt     # una consulta por línea, # para comentarios
flacpipe pedir --cancion "Steve Lacy - Dark Red"

flacpipe estado            # cola y progreso
flacpipe estado --seguir   # repinta cada 5 s con el log al pie
flacpipe cancelar "Radiohead"
flacpipe limpiar           # quita de la cola lo ya terminado
```

También desde cualquier ventana de chat de Nicotine+: `/flac Artista - Álbum`,
`/flacs`, `/flacpausa`.

El formato `Artista - Álbum` es el recomendado: el artista y el álbum se guardan
por separado y sirven de respaldo si los archivos vienen con los tags rotos.

### Qué hace el plugin con cada petición

Procesa **una a la vez, en el orden en que las pediste**:

1. Lanza una búsqueda global añadiendo `flac` al término si no lo lleva.
2. Acumula resultados durante 45 s (mínimo 10 s; si no llega nada, espera hasta 3
   ventanas antes de rendirse).
3. Agrupa los `.flac` por carpeta de origen y puntúa cada fuente.
4. Descarga la mejor a `~/Música/.descargas/<id>/`, aislada del resto.
5. Lee los tags y mueve todo a `~/Música/Artista/Álbum/NN Título.flac`.
6. Pasa a la siguiente petición.

### Cómo elige la fuente

| Criterio | Efecto |
|---|---|
| Profundidad de bits | 24 → +130 · 32 → +115 · 20 → +90 · 16 → +40 |
| Fuera del rango de bits | **descartada** |
| Sample rate | `min(tasa / 4000, 56)` — 48 kHz → +12, 96 kHz → +24, 192 kHz → +48 |
| No declara calidad | **−60**, y proporción de archivos con datos hasta +20 |
| Nº de pistas | `min(n × 3, 45)`; menos del mínimo → **descartada** |
| Velocidad del peer | hasta +30 |
| Slots libres / cola | +25 / hasta −30 |
| Coincidencia con lo pedido | hasta +30 |
| Parece una discografía | −70 |

Un 32-bit puntúa por debajo de un 24-bit a propósito: en Soulseek casi siempre es
un 24-bit reempaquetado. Y una fuente que **no declara** calidad nunca gana a una
que declara 24-bit, aunque sea más rápida.

### Qué pasa cuando algo va mal

- **La fuente se cuelga.** No se mide el tiempo total sino el **avance**: si no se
  mueve un solo byte durante 10 minutos, se descarta esa fuente y se baja el álbum
  entero de la siguiente candidata. Un álbum grande desde un peer lento es normal;
  uno que no avanza, no. (`timeout_descarga`, 4 h, es sólo una red de seguridad.)
- **Falta una pista.** Un álbum incompleto no se acepta: se cambia de fuente. Sólo
  tras agotar `max_intentos` se guarda lo que haya, marcado como incompleto.
- **Huecos en la numeración.** Contar archivos no detecta que falte la 04. Se
  comprueba la secuencia, y si los tags traen `TRACKTOTAL` también se detecta que
  falte la última. `flacpipe revisar` los busca en toda la biblioteca.
- **Nicotine+ se cierra a medias.** Al arrancar, el plugin devuelve a `pendiente`
  lo que quedó en curso y lo retoma.

### Descargas manuales

Si bajas algo a mano desde Nicotine+, cae en `~/Música/.descargas`. Para meterlo
en la biblioteca con la nomenclatura correcta:

```bash
flacpipe organizar --simular   # ver el plan
flacpipe organizar
```

Funciona con carpetas y con **archivos sueltos**: los agrupa por `ALBUMARTIST` y
`ALBUM` antes de organizarlos, así que una pista suelta cae en su álbum.

## Parte 2 — copiar al FiiO

```bash
flacpipe fiio --simular          # plan completo sin escribir un byte
flacpipe fiio                    # copia todo lo nuevo
flacpipe fiio "Radiohead" "Nujabes/Modal Soul"
flacpipe fiio --rehacer "Nujabes/Modal Soul"
flacpipe fiio --desmontar
```

Detecta la tarjeta sola buscando montajes FAT/exFAT/NTFS bajo `/run/media`,
`/media` y `/mnt`. Copia un archivo a la vez, con `fsync` de cada uno y `sync`
tras cada álbum, para que la entrada de directorio quede escrita antes de crear la
siguiente. La carátula se copia al final, para no ocupar una entrada antes de las
pistas.

Si la carpeta del artista **ya existe** en la tarjeta, el álbum nuevo entra dentro;
no se duplica. Si no existe, se crea.

Al terminar cada álbum relee el directorio y comprueba que el orden físico coincide
con el de pistas. Esa comprobación sólo se hace en FAT/exFAT, donde el orden de
`listdir` es el de la tabla de directorios; en ext4 los directorios son hash y no
dice nada, así que se salta sola.

| Opción | Para qué |
|---|---|
| `--destino /ruta` | Punto de montaje, si la detección no acierta |
| `--subcarpeta Music` | Carpeta dentro de la tarjeta (por defecto `Artistas`) |
| `--raiz ~/Música/Artistas` | Copiar otra biblioteca de origen |
| `--rehacer` | Borrar y reescribir álbumes que ya estén, para forzar el orden |
| `--simular` / `-n` | Sólo enseñar el plan |
| `--si` / `-y` | No preguntar |
| `--desmontar` | `sync` y desmontar al terminar |

Si un álbum ya está en la tarjeta pero desordenado (copiado antes arrastrándolo),
`--rehacer` lo borra y lo reescribe: en FAT/exFAT no hay otra forma de reordenar
las entradas de un directorio.

## Estructura

| Ruta | Qué es |
|---|---|
| `flac_hunter/__init__.py` | El plugin: cola, búsqueda, puntuación, descarga, reintentos |
| `flac_hunter/fh_comun.py` | Rutas, cola JSON con `flock`, log, detección de Nicotine+ |
| `flac_hunter/fh_biblioteca.py` | Tags FLAC, STREAMINFO, nombres seguros, organización, huecos |
| `flac_hunter/fh_fiio.py` | Copia secuencial y verificación del orden físico |
| `flac_hunter/PLUGININFO` | Metadatos que lee Nicotine+ |
| `flacpipe` | CLI |
| `instalar.sh` | Instalador |

Los módulos compartidos llevan prefijo `fh_` porque Nicotine+ carga los plugins
haciendo `sys.path.append(carpeta_del_plugin)`: los imports son absolutos y un
nombre genérico chocaría con otro módulo del intérprete.

Estado en tiempo de ejecución, fuera del repo:

- `~/Música/.pipeline/cola.json` — la cola (escritura atómica, `flock`)
- `~/Música/.pipeline/pipeline.log` — log del pipeline, rotado a 1 MB
- `~/Música/.descargas/` — carpetas temporales, una por petición

## Ajustes del plugin

En Nicotine+ → Preferencias → Plugins → FLAC Hunter:

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `pausado` | `False` | No toma peticiones nuevas |
| `bits_minimos` | `16` | Por debajo, la fuente se descarta |
| `bits_maximos` | `32` | El máximo real de FLAC son 32 bits |
| `ventana_busqueda` | `45` | Segundos acumulando resultados (suelo de 10) |
| `min_pistas_album` | `4` | Mínimo para aceptar una carpeta como álbum |
| `max_intentos` | `3` | Fuentes distintas antes de darla por fallida |
| `timeout_sin_avance` | `600` | Segundos sin avanzar un byte antes de cambiar de fuente |
| `timeout_descarga` | `14400` | Tope absoluto por álbum |
| `añadir_flac_a_consulta` | `True` | Añade `flac` al término de búsqueda |
| `verboso` | `True` | Registra cada candidato con su puntuación |

## Detalles de diseño

- **No se usa la wishlist de Nicotine+.** Sólo lanza un término por intervalo
  (~12 min sin privilegios, lo dicta el servidor) y no da control del orden. El
  plugin mantiene su propia cola y llama a `core.search.do_search()` directamente.
- **`enqueue_download(folder_path=…)`, no `enqueue_folder()`.** `folder_path` es el
  destino local, así que cada petición baja a su propia carpeta y los álbumes no se
  mezclan. `enqueue_folder` depende de que el peer conteste a un
  `FolderContentsRequest`.
- **Álbumes dobles**: se numeran de corrido (disco 1: 01–12, disco 2: 13–24) dentro
  de una sola carpeta, para que el FiiO reproduzca el álbum entero en orden.
- **Caracteres ilegales en FAT** (`\ / : * ? " < > |`) se sustituyen por homoglifos
  Unicode (`⁄ ꞉ ✱ ？ ＂ ＜ ＞ ｜`), así los títulos siguen leyéndose en pantalla.
- **Confinamiento del snap**: la interfaz `home` permite `@{HOME}/[^.]*/**`, o sea
  carpetas ocultas *dentro* de `~/Música` sí, pero no colgando directamente de
  `$HOME`. Además, dentro del snap `$HOME` es `~/snap/nicotine-plus/<rev>/`, así que
  todas las rutas compartidas son absolutas.
- **Los errores de un plugin los traga `events.emit`**, que llama a
  `show_plugin_error` y sigue. Por eso todo el ciclo va envuelto en `try/except`
  con log propio: si no, el plugin fallaría en silencio.

## Limitaciones conocidas

- Rutas fijas para un solo usuario; no hay archivo de configuración.
- El plugin sólo avanza mientras Nicotine+ está abierto.
- Si a un álbum le faltan pistas y ninguna fuente lo tiene completo, se guarda
  incompleto tras 3 intentos (queda marcado, y `flacpipe revisar` lo lista).
- Al reintentar se vuelve a bajar el álbum entero de otra fuente, no sólo las
  pistas que faltan.
