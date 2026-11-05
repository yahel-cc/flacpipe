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

- Nicotine+ 3.3.x con una cuenta de Soulseek, en **Linux** (snap, flatpak o
  paquete nativo; probado con el snap 3.3.10), **macOS** (la app) o **Windows**
  (el instalador oficial).
- Python 3.8+ para la CLI y la interfaz. **Sin dependencias externas**: los tags
  Vorbis y el bloque STREAMINFO se leen a mano, porque el plugin corre dentro del
  Python de Nicotine+ (el del snap, el del `.app` o el del `.exe`) y ahí no hay
  pip. La única excepción es Windows, donde la interfaz necesita
  `windows-curses`; el instalador lo pone.

## Instalación

```bash
git clone git@github.com:yahel-cc/flacpipe.git
cd flacpipe
bash instalar.sh      # Linux y macOS, con Nicotine+ CERRADO
instalar.bat          # Windows: doble clic, o desde una terminal
```

Los dos llaman a `instalar.py`, que sólo usa la biblioteca estándar y hace,
en este orden:

1. localiza Nicotine+: el snap, el flatpak o el paquete nativo en Linux, la app
   en macOS, `%APPDATA%\nicotine` en Windows. Si hay varios, gana el que haya
   escrito su config más recientemente; si ninguno se ha abierto aún, el que
   esté instalado;
2. decide dónde está la biblioteca (la carpeta de música del sistema: `xdg-user-dir
   MUSIC` en Linux, `~/Music` en macOS y Windows) y la escribe en
   `flac_hunter/flacpipe.json`, que es lo único que leen el plugin, la CLI y la
   interfaz para saber dónde trabajar;
3. enlaza `flac_hunter/` en la carpeta de plugins de Nicotine+ (**enlace
   simbólico**, o una *junction* en Windows, que no pide permisos de
   administrador: editas el código aquí y basta reiniciar Nicotine+). Si no se
   puede enlazar, copia, y entonces hay que reinstalar tras cada cambio;
4. añade `flac_hunter` a los plugins habilitados de Nicotine+ y apunta
   `downloaddir` a `<biblioteca>/.descargas`, para que una descarga manual no
   ensucie la raíz de la biblioteca. La config original queda al lado como
   `config.antes-de-flacpipe`. Si Nicotine+ nunca se ha abierto, escribe una
   config mínima con sólo eso, que Nicotine+ completa al arrancar;
5. deja el comando `flacpipe` a mano: enlace en `~/.local/bin` (y se ofrece a
   añadirlo al PATH en tu `.zshrc`/`.bashrc` si no está) o
   `%LOCALAPPDATA%\flacpipe\bin\flacpipe.cmd` más el PATH del usuario en Windows;
6. en Windows, `pip install windows-curses`;
7. crea `.pipeline` y `.descargas` dentro de la biblioteca y comprueba con un
   intérprete limpio que todo importa y que el plugin instalado responde.

Se puede repetir las veces que haga falta (también tras mover la carpeta del
código, para repuntar los enlaces). Opciones:

| Opción | Para qué |
|---|---|
| `--biblioteca RUTA` | La música no está en la carpeta habitual |
| `--simular` / `-n` | Enseñar qué haría sin tocar nada |
| `--quitar` | Desinstalar: quita el plugin, el comando y `flacpipe.json`; no toca la música ni la cola |
| `--copiar` | Copiar el plugin en vez de enlazarlo |
| `--nicotine-datos DIR` | Carpeta de datos de Nicotine+ (la que contiene `plugins/`) si la detección no acierta |
| `--si` / `-y` | No preguntar nada |

### Dónde está la biblioteca

`flac_hunter/flacpipe.json` (lo escribe el instalador, no va en git):

```json
{
  "biblioteca": "/home/yahel/Música",
  "nicotine": {"sabor": "snap", "config": "…/config", "datos": "…/nicotine"},
  "ignorar": []
}
```

`ignorar` son carpetas de primer nivel de la biblioteca que no son artistas
(además de `.pipeline`, `.descargas`, `Artistas` y lo que dejan iTunes y
compañía en `~/Music`). La variable de entorno `FLACPIPE_BIBLIOTECA` manda
sobre el archivo, pero sólo la ve la CLI: el plugin, confinado, lee el JSON.
Vive junto al código y no en `~/.config` porque es el único sitio que ven a la
vez el plugin dentro del snap y la CLI fuera.

## La interfaz

```bash
flacpipe          # sin argumentos: interfaz a pantalla completa
flacpipe tui      # lo mismo, explícito
```

Cinco vistas, una por tecla, y **la última fila dice siempre qué hace cada tecla
en la vista en la que estás**. No hay nada que memorizar.

| tecla | vista | para qué |
|---|---|---|
| `1` | Cola | las peticiones, con la fuente que ganó y las pistas cayendo una a una; `i` importa una playlist en CSV |
| `2` | Biblioteca | artista → álbum → pistas en columnas; marcar lo que va a la tarjeta |
| `3` | FiiO | la tarjeta, el plan de copia y el orden físico verificándose en vivo |
| `4` | Log | `pipeline.log` en directo, con la etiqueta coloreada y filtro |
| `5` | Salud | `revisar` como lista, con la tecla que arregla cada cosa debajo |

La navegación es **con flechas**; no hay atajos estilo vim. `?` abre la ayuda
entera, `:` una paleta con todas las órdenes buscando por nombre, `q` sale.

En la cola, el panel de la derecha **se reparte según el sitio que haya**: la
ficha va a dos columnas cuando cabe y debajo aparecen las **pistas** del álbum
(mientras baja, cayendo una a una, con la que está en curso y sus bytes
subiendo), la **tabla de fuentes** que puntuó el plugin (la elegida, las
descartadas y por cuántos puntos) y la **historia** de esa petición sacada de
`pipeline.log`. Con la terminal muy ancha, pistas a un lado y fuentes e
historia al otro; con poca altura se quedan sólo los campos que pesan. Todo lo
que se mueve mide algo real: el medidor VU y la chispa salen de los bytes por
segundo, la cuenta de la búsqueda son los 45 s que el plugin acumula
resultados, y las peticiones en espera «respiran». Cuando un álbum termina, un
aviso entra deslizándose por la derecha y la fila parpadea en verde.

La biblioteca **se relee sola** cuando el plugin organiza un álbum: cada pocos
segundos se compara el mtime de la raíz y el de cada carpeta de artista, y si
algo cambió se recarga sin perder dónde estabas (la posición se recuerda por
nombre, no por índice). **La esquina derecha de las pestañas dice siempre qué
biblioteca estás mirando**: si un álbum recién bajado «no aparece», casi siempre
es que estabas en la heredada.

### Teclas, pestaña por pestaña

Lo mismo que muestran la última fila y `?`, con lo que hace cada tecla de
verdad. Las mayúsculas cuentan: `L` y `R` no son `l` ni `r`.

#### En todas partes

| tecla | qué hace |
|---|---|
| `1` … `5` | ir a Cola, Biblioteca, FiiO, Log o Salud |
| `⇥` / `⇧⇥` | pestaña siguiente / anterior |
| `?` | ayuda: las teclas generales y las de la pestaña en la que estás. Cualquier tecla la cierra |
| `:` | paleta de órdenes: escribe parte del nombre, elige con `↑↓` y ejecuta con `↵`. Junta órdenes de todas las pestañas, así que se puede, por ejemplo, copiar al FiiO desde la Cola. Las que no tienen tecla propia (pedir una canción suelta, abrir Nicotine+, abrir `pipeline.log` en `$EDITOR`) sólo están aquí |
| `s` | para la música que esté sonando, estés en la pestaña que estés |
| `q` | salir. Si hay una tarea corriendo (copia, organizar, expulsar…) pregunta antes |

Con una ventana emergente abierta (pedir, buscar, confirmar, paleta…), las
teclas van a esa ventana y no a la pestaña: `esc` la cierra.

#### 1 · Cola

A la izquierda, las peticiones activas y las 8 últimas terminadas; a la
derecha, la ficha de la seleccionada.

| tecla | qué hace |
|---|---|
| `↑↓` | moverse por las peticiones. `RePág`/`AvPág` saltan de 5 en 5; `Inicio`/`Fin` van a la primera y a la última |
| `a` | pedir algo: abre una ventana para escribir qué buscar («Artista - Álbum» funciona mejor que un título suelto). `⇥` cambia entre álbum y canción, `↵` lo encola, `esc` cancela |
| `i` | importar una playlist en CSV (Exportify, TuneMyMusic…). Primero eliges el archivo con `↑↓` y `↵`; luego ves el plan: qué se va a pedir y qué ya tienes. Ahí `⇥` cambia entre pedir las canciones o los álbumes completos, `r` decide si se piden también las que ya tienes o que ya están en la cola (por defecto se saltan), `↵` encola y `esc` vuelve a la lista de archivos |
| `x` | cancelar la petición seleccionada. Sólo sirve con las que siguen activas |
| `t` | alternar entre ver sólo lo activo y lo reciente o **toda** la historia de la cola, de lo más nuevo a lo más viejo |
| `L` | limpiar: saca de la cola todo lo terminado (listo, fallido o cancelado). Lo activo no se toca |
| `o` | organizar `.descargas`: lo que el plugin no llegó a archivar pasa a `~/Música/Artista/Álbum`. Los FLAC sueltos se agrupan antes por sus tags. El progreso se ve aquí mismo |
| `c` | ir a la pestaña FiiO |

#### 2 · Biblioteca

Tres columnas: artistas → álbumes → pistas.

| tecla | qué hace |
|---|---|
| `←→` | cambiar de columna |
| `↑↓` | moverse dentro de la columna. `RePág`/`AvPág` saltan de 10 en 10, `Inicio` va arriba. Al cambiar de artista o de álbum, las columnas de la derecha vuelven al principio |
| `espacio` | marcar o desmarcar el álbum del cursor (lleva un `▸`). Los marcados son lo que se copia a la tarjeta y lo que `h` mueve; la barra de estado dice cuántos llevas |
| `c` | ir a la pestaña FiiO para copiar. Si no habías marcado nada, antes marca el álbum del cursor, así que copia sólo ese y no la biblioteca entera |
| `p` | reproducir la pista seleccionada con el primer reproductor que encuentre (`mpv`, `gst-play-1.0`, `pw-play`, `afplay`, `ffplay` o `mplayer`) |
| `s` | parar la reproducción |
| `/` | filtrar artistas por nombre mientras escribes. `↵` deja el filtro puesto; `esc` lo quita |
| `r` | releer la biblioteca del disco ahora, sin esperar a que se relea sola |
| `b` | cambiar entre la biblioteca nueva (`~/Música`) y la heredada (`~/Música/Artistas`). Sólo cambia lo que ves; no mueve nada. El cursor vuelve al principio |
| `h` | **mover** a la heredada, la carpeta que comparte Nicotine+. Mueve lo marcado; si no hay marcas, el artista entero cuando el cursor está en la columna de artistas, o el álbum del cursor en las otras. Pide confirmación, no pisa un álbum que ya esté en la heredada y borra la carpeta del artista en la nueva si queda vacía. Lo movido deja de salir en la biblioteca nueva y en la copia normal a la FiiO: para copiarlo, cambia antes con `b` a la heredada. Mirando la heredada, `h` no hace nada |
| `o` | organizar `.descargas`, igual que en la Cola. Te lleva a la Cola para ver el progreso |

#### 3 · FiiO

La tarjeta montada y el plan de copia: los álbumes marcados o, si no hay
marcas, toda la biblioteca que estés mirando (nueva o heredada). Se copia a
`Artistas/` dentro de la tarjeta, álbum por álbum y pista por pista, en orden.

| tecla | qué hace |
|---|---|
| `↑↓` | elegir la tarjeta, si hay más de una montada |
| `↵` o `c` | copiar. Antes comprueba que hay tarjeta, que hay algo que copiar y que cabe, y pide confirmación. Los álbumes que ya están completos y en orden se saltan; los que están incompletos o en otro orden **no se tocan**, sólo avisa de ellos (para eso está `w`) |
| `n` | simular: enseña lo que copiaría, pista por pista, sin escribir nada |
| `w` | rehacer: como `↵`, pero los álbumes que ya estén en la tarjeta se **borran y se reescriben** en orden. Es la única forma de arreglar el orden en FAT/exFAT. Pide confirmación |
| `m` | quitar todas las marcas; el plan vuelve a ser toda la biblioteca |
| `u` | expulsar la tarjeta: sincroniza lo pendiente y la desmonta para que se pueda sacar sin peligro |
| `r` | volver a calcular el plan |
| `x` | sólo mientras copia: abortar después de la pista en curso. El álbum a medias queda marcado como desordenado; arréglalo después con `w` |

#### 4 · Log

`pipeline.log` en directo, con las etiquetas coloreadas.

| tecla | qué hace |
|---|---|
| `↑↓` | moverse línea a línea. `RePág`/`AvPág` saltan de 10 en 10. Al moverte se deja de seguir el final |
| `Inicio` | ir a la primera línea |
| `Fin` | ir al final y volver a seguirlo |
| `f` | alternar entre seguir el final (cada línea nueva aparece abajo) y fijar la vista donde está |
| `/` | dejar sólo las líneas que contengan el texto. `↵` lo aplica; `esc` lo quita |

#### 5 · Salud

Lo mismo que `flacpipe revisar`, como lista: la instalación del plugin,
Nicotine+, la cola y las descargas, los álbumes incompletos y la tarjeta. Cada
problema lleva debajo la tecla que lo arregla.

| tecla | qué hace |
|---|---|
| `R` | volver a revisar todo |
| `o` | organizar `.descargas` (te lleva a la Cola para ver el progreso) |
| `p` | volver a pedir el primer álbum incompleto de la lista |
| `n` | abrir Nicotine+ si no está corriendo |
| `3` | ir a la pestaña FiiO (es la tecla general de pestañas) |

#### Confirmaciones

Cuando algo pregunta «¿…?»: `s`, `y` o `↵` dicen que sí; `n`, `q` o `esc`, que
no.

La CLI sigue entera y es la que hay que usar en scripts o con tuberías: la
interfaz no la sustituye, sólo evita tener que recordarla.

Se dibuja con `curses`, de la biblioteca estándar. La copia al FiiO y el
organizador corren en un hilo aparte, así que la pantalla no se congela ni
durante un `fsync` de 60 MB.

## Comprobar que funciona

```bash
flacpipe revisar            # instalación, plugin, cola, álbumes incompletos, tarjeta
flacpipe revisar --prueba   # además organiza un álbum falso, sin tocar la red
```

Lo mismo se ve en la vista `5` de la interfaz, que además ofrece la tecla que
arregla cada problema.

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
flacpipe pedir --csv "Mi playlist.csv"      # una playlist exportada de Spotify

flacpipe estado            # cola y progreso
flacpipe estado --seguir   # repinta cada 5 s con el log al pie
flacpipe cancelar "Radiohead"
flacpipe limpiar           # quita de la cola lo ya terminado
```

También desde cualquier ventana de chat de Nicotine+: `/flac Artista - Álbum`,
`/flacs`, `/flacpausa`.

El formato `Artista - Álbum` es el recomendado: el artista y el álbum se guardan
por separado y sirven de respaldo si los archivos vienen con los tags rotos.

### Playlists de Spotify

Spotify no exporta nada, pero [Exportify](https://exportify.net), TuneMyMusic
o Soundiiz sacan una playlist como CSV. `flacpipe pedir --csv archivo.csv` lo
lee y pide **cada canción por separado**, como `--cancion`: el plugin busca
«Artista - Canción flac», baja sólo esa pista y la archiva por sus tags en el
álbum al que pertenece. Antes de encolar enseña el plan y salta lo que **ya
está en la biblioteca** (por nombre de carpeta y de archivo, en la nueva y en la
heredada) y lo que ya está en la cola.

```bash
flacpipe pedir --csv "Mi playlist.csv" --simular   # sólo el plan
flacpipe pedir --csv "Mi playlist.csv"             # encolar
flacpipe pedir --csv "Mi playlist.csv" --albumes   # los discos completos, una vez cada uno
flacpipe pedir --csv "Mi playlist.csv" --repetir   # también lo que ya tienes
```

Las columnas se adivinan por la cabecera, así que sirve cualquier CSV con
artista y título (y álbum, si lo hay), separado por comas, punto y coma o
tabulador. De los artistas se queda con el principal, porque Soulseek exige que
todas las palabras de la búsqueda estén en la ruta y un «feat.» sólo estorba;
por lo mismo quita «(feat. …)» y «- Remastered 2009» del título. Cada petición
recuerda de qué lista vino, y la ficha de la interfaz lo enseña.

Cuenta unos 2 minutos por canción: 45 s de búsqueda más la descarga, una tras
otra. Una playlist de 50 canciones son casi dos horas con Nicotine+ abierto.

En la interfaz es la tecla `i` en la Cola: elige el CSV con las flechas entre
los que haya en Descargas, Música, Documentos o el escritorio, mira el plan y
encola con ↵. `⇥` cambia entre canciones y álbumes completos.

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

Detecta la tarjeta sola buscando montajes FAT/exFAT/NTFS: bajo `/run/media`,
`/media` y `/mnt` en Linux, en `/Volumes` en macOS, y entre las unidades
extraíbles (o fijas pero FAT, que es como aparecen algunos lectores) en
Windows. Copia un archivo a la vez, con `fsync` de cada uno y `sync` tras cada
álbum, para que la entrada de directorio quede escrita antes de crear la
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
| `--desmontar` | `sync` y desmontar al terminar (`udisksctl`, `diskutil` o la expulsión de Windows) |

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
| `flac_hunter/fh_listas.py` | Playlists en CSV → peticiones; qué se tiene ya |
| `flac_hunter/PLUGININFO` | Metadatos que lee Nicotine+ |
| `flac_hunter/flacpipe.json` | Dónde están la biblioteca y Nicotine+ (lo genera el instalador; no va en git) |
| `flacpipe` | CLI |
| `flacpipe_tui.py` | La interfaz a pantalla completa (curses) |
| `instalar.py` | Instalador para los tres sistemas; `instalar.sh` e `instalar.bat` sólo lo lanzan |
| `pruebas/probar.py` | Banco de pruebas de la interfaz: la corre en un pty y captura la pantalla |

La interfaz vive fuera de `flac_hunter/` a propósito: nunca la carga Nicotine+,
sólo lee `cola.json` y `pipeline.log` con el mismo `flock` que todo lo demás, y
lanza el trabajo pesado en un hilo. **El plugin no sabe que existe.**

Para ver la interfaz sin abrirla, `python3 pruebas/probar.py` la lanza dentro
de un pty en tres tamaños de terminal, le manda teclas y guarda capturas de
texto (con un mapa de colores al lado) en `pruebas/salida/capturas/`. Con
`real` usa tu cola de verdad, sólo lectura; con `falso` simula una descarga
que avanza, una búsqueda, una espera y un álbum que termina; con `fiio`, una
copia a una tarjeta inventada. No escribe nada en `~/Música`.

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
  todas las rutas compartidas son absolutas (el HOME real está en
  `SNAP_REAL_HOME`). El flatpak, en cambio, tiene `--filesystem=home` entero.
- **Lo que cambia por sistema vive en `fh_comun` y `fh_fiio`**: el bloqueo de la
  cola (`flock` o `msvcrt.locking`), cómo se encuentra el proceso de Nicotine+
  (`/proc`, `ps` o `tasklist`), cómo se abre, y cómo se listan los montajes
  (`/proc/mounts`, `mount` o `GetLogicalDrives`). El resto del código no
  pregunta en qué sistema está.
- **Los errores de un plugin los traga `events.emit`**, que llama a
  `show_plugin_error` y sigue. Por eso todo el ciclo va envuelto en `try/except`
  con log propio: si no, el plugin fallaría en silencio.

## Limitaciones conocidas

- En macOS y Windows el código está portado pero **sin probar en una máquina
  real**: sólo se ha ejecutado en Linux (snap). Lo más probable que falle allí es
  la detección de la tarjeta y la interfaz en Windows, que depende de
  `windows-curses` y de que la terminal pinte los glifos (Windows Terminal sí).
- En Windows la tecla `p` de la biblioteca sólo suena si hay `mpv` o `ffplay`
  en el PATH; en macOS usa `afplay`, que viene de serie.
- El plugin sólo avanza mientras Nicotine+ está abierto.
- Si a un álbum le faltan pistas y ninguna fuente lo tiene completo, se guarda
  incompleto tras 3 intentos (queda marcado, y `flacpipe revisar` lo lista).
- Al reintentar se vuelve a bajar el álbum entero de otra fuente, no sólo las
  pistas que faltan.
