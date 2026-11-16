# filegraph

Servidor MCP en Python 3.12 que escanea directorios y analiza la estructura de archivos para reorganización, limpieza y detección de patrones.

## Qué resuelve

Cuando trabajás con colecciones de archivos (logos, fotos, documentos, assets), necesitás responder preguntas como:

- **¿Qué hay en esta carpeta?** → `scan_directory` te da el árbol completo
- **¿Cuántos archivos .svg tengo?** → `search_by_type` filtra por extensión
- **¿Tengo archivos duplicados?** → `find_duplicates` los agrupa por hash
- **¿Hay patrones en los nombres?** → `find_patterns` detecta secuencias, prefijos y sufijos
- **¿Cuánto pesa cada archivo?** → `get_file_metadata` te da tamaño, permisos y fechas

filegraph se integra directamente con MCP (Model Context Protocol) para que agentes de IA puedan explorar y analizar directorios de forma autónoma, sin necesidad de navegar el sistema de archivos manualmente.

## Instalación

### Instalación rápida (recomendado)

```bash
curl -fsSL https://raw.githubusercontent.com/nahuelhollmann109/filegraph/main/install.sh | bash
```

### Qué hace el instalador

1. Muestra un **resumen de todo lo que va a hacer** antes de cambiar nada (con `--dry-run` lo podés ver sin ejecutar ninguna acción).
2. Clona o actualiza el repo en `~/.local/share/filegraph`.
3. Instala las dependencias Python con la primera estrategia que funcione: `uv` ya instalado → venv del sistema → `pip` del sistema → **bootstrap de uv en user-space** (descarga el binario fijado de su release oficial y verifica el checksum).
4. Detecta tu config de OpenCode (`$OPENCODE_CONFIG`, `opencode.json` o `opencode.jsonc`), crea un **backup una sola vez** (`<config>.filegraph.bak`) y registra el MCP con la ruta **absoluta** del intérprete con el que instaló las deps.
5. Corre un **smoke test** (el server tiene que arrancar) antes de anunciar éxito. Si algo falla, lo dice con la causa y la solución — nunca reporta éxito falso.

Todo pasa dentro de su directorio de instalación: **sin sudo, sin paquetes del sistema, sin tocar tus profiles de shell ni tu PATH**. El comando `uninstall` revierte todo lo creado.

### Requisitos

- `git`, `jq` y `python3` (≥ 3.12). Si falta alguno, el instalador imprime el comando exacto para tu distro (apt/dnf/yum/pacman/zypper/apk).
- No hace falta `pip` ni `python3-venv`: si el sistema no tiene ningún instalador usable, el instalador se trae su propio `uv`.

### Variables y opciones

| Variable / flag | Qué hace |
|-----------------|----------|
| `--dry-run` | Solo imprime el resumen de acciones; no cambia nada |
| `OPENCODE_CONFIG` | Ruta explícita a la config de OpenCode (si el archivo existe, tiene prioridad; si no, se detecta `.json`/`.jsonc`) |
| `FILEGRAPH_INSTALL_DIR` | Cambia el directorio de instalación (default: `~/.local/share/filegraph`) |
| `FILEGRAPH_PYTHON` / `--python <path>` | Intérprete que registra `setup-mcp.sh` (default: el que instaló las deps) |

### Instalación manual

```bash
git clone https://github.com/nahuelhollmann109/filegraph.git
cd filegraph
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
FILEGRAPH_PYTHON="$PWD/.venv/bin/python" ./scripts/setup-mcp.sh install
```

### Desinstalar

```bash
# Si usaste curl
bash ~/.local/share/filegraph/install.sh uninstall

# Si instalaste manualmente
./scripts/setup-mcp.sh uninstall
```

Elimina el server de OpenCode y borra todo su directorio de instalación (repo, venv y uv). El backup `<config>.filegraph.bak` se conserva a propósito.

### Actualizar una instalación existente

Volvé a correr el instalador (o `./scripts/setup-mcp.sh install`). No hace falta
`uninstall` primero, y de hecho no lo uses como atajo: `uninstall` **borra el
directorio de instalación** con el repo y el venv adentro.

El instalador compara la entrada ya registrada con la que debería existir. Si el
módulo o el `cwd` no coinciden — por ejemplo una entrada vieja con `-m src.main`
de antes de que el paquete se llamara `filegraph` — **la reescribe en el lugar** y
te dice qué cambió:

```
Updating the existing FileGraph MCP server entry.
   module: src.main -> filegraph.main
   cwd:    /clon/viejo -> /clon/actual
```

Si ya coincide, no toca nada. El backup `<config>.filegraph.bak` se crea **una
sola vez** y nunca se sobrescribe, así que el original prístino queda intacto por
más reinstalaciones que hagas. `./scripts/setup-mcp.sh status` avisa cuando la
entrada registrada arranca un módulo distinto al de esta versión, o cuando su
`command` no tiene la forma esperada.

Si editaste la entrada a mano y `command` quedó como texto en vez de array
(`"command": "python3 -m src.main"`), el instalador también la repara: te muestra
el valor que encontró y la reescribe como `[intérprete, -m, módulo]`. Los demás
campos de la entrada (`env`, `enabled`, `description`, …) se conservan.

### Si tu entrada todavía dice `-m src.main`

El módulo del server es `filegraph.main` y el paquete `src` **ya no existe**: una
entrada vieja no arranca. Volvé a correr el instalador (o
`./scripts/setup-mcp.sh install`) y la repara en el lugar, diciéndote qué cambió
— ese es el único camino de migración que queda. `./scripts/setup-mcp.sh status`
te avisa si la entrada registrada arranca un módulo que esta versión no trae.

Solo `filegraph` viaja en el wheel (`packages = ["filegraph"]`): el `src` de
nivel superior que publicaban las versiones viejas no vuelve, y algo importable
como `src` desde `site-packages` no está soportado.

## Configurar en un cliente MCP

Agregar esta configuración a tu cliente MCP (Claude Desktop, etc.). Usá siempre la ruta **absoluta** del intérprete — los clientes MCP no heredan tu PATH ni un venv activo:

```json
{
  "mcpServers": {
    "filegraph": {
      "command": "/ruta/a/filegraph/.venv/bin/python",
      "args": ["-m", "filegraph.main"],
      "cwd": "/ruta/a/filegraph"
    }
  }
}
```

Si instalaste con curl, la ruta exacta aparece al final del instalador.

### Pi

Pi lee los servers de `~/.pi/agent/mcp.json` (global) o de `.pi/mcp.json` en el
proyecto. Ojo con un detalle: el `exposure` por defecto es `codemode`, que **no**
declara las tools al modelo — quedan alcanzables solo desde scripts de codemode.
Para que aparezcan como herramientas normales hay que pedir `direct`.

```bash
pi mcp add filegraph \
  --cwd /ruta/a/filegraph \
  --exposure direct \
  --description "Directory scanning: directory trees, search by extension, duplicate files by hash, naming patterns, file metadata, and a SQLite scan cache" \
  -- /ruta/a/filegraph/.venv/bin/python -m filegraph.main
```

La entrada que queda escrita es esta:

```json
{
  "mcpServers": {
    "filegraph": {
      "command": "/ruta/a/filegraph/.venv/bin/python",
      "args": ["-m", "filegraph.main"],
      "cwd": "/ruta/a/filegraph",
      "exposure": "direct",
      "description": "Directory scanning: directory trees, search by extension, duplicate files by hash, naming patterns, file metadata, and a SQLite scan cache"
    }
  }
}
```

`command` es **un ejecutable**, no una cadena de shell: de ahí la ruta absoluta
del intérprete y los argumentos por separado.

Para verificar, `pi mcp list` imprime el estado y la cantidad de tools de cada
server, y sale con código 1 si alguno habilitado no conecta. Si editaste el
archivo con Pi ya abierto, corré `/reload` en la sesión. Las tools quedan
disponibles como `mcp__filegraph__scan_directory`,
`mcp__filegraph__find_duplicates`, etc.

## Tools de escaneo

### `scan_directory`

Escanea un directorio y devuelve su jerarquía como árbol JSON.

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Ruta del directorio a escanear |
| `max_depth` | int | ❌ | Profundidad máxima (None = sin límite) |
| `exclude_dirs` | list[str] | ❌ | Directorios a excluir |
| `include_excluded` | bool | ❌ | Incluir directorios excluidos por defecto |
| `max_seconds` | float | ❌ | Presupuesto de tiempo (ver [Mounts de red](#mounts-de-red)) |
| `max_entries` | int | ❌ | Tope de entradas (ver [Mounts de red](#mounts-de-red)) |

**Ejemplo:**

```json
{
  "tree": {
    "type": "directory",
    "name": "Logos-html",
    "children": [
      {"type": "file", "name": "logo.svg", "size": 68936},
      {"type": "file", "name": "icon.png", "size": 47672}
    ]
  },
  "warnings": [],
  "truncated": null
}
```

`truncated` es `null` cuando el escaneo cubrió todo el árbol. Si el presupuesto lo
cortó, trae `{"reason", "limit", "entries_seen"}` con `reason` en `time_budget` o
`entry_budget`: el árbol devuelto es parcial y está dicho, nunca presentado como
si estuviera completo.

### `search_by_type`

Busca archivos por extensión bajo una ruta específica.

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Directorio raíz para buscar |
| `file_type` | str | ✅ | Extensión a filtrar (ej: `jpg`, `png`, `pdf`) |
| `exclude_dirs` | list[str] | ❌ | Directorios a excluir |
| `include_excluded` | bool | ❌ | Incluir directorios excluidos por defecto |

**Ejemplo:**

```json
{
  "results": [
    {"name": "photo.jpg", "path": "/home/user/fotos/photo.jpg", "size": 1234567}
  ]
}
```

### `find_duplicates`

Encuentra archivos duplicados agrupados por hash (SHA256 por defecto).

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Directorio a analizar |
| `hash_algo` | str | ❌ | Algoritmo: sha256, md5 (default: sha256) |
| `min_size` | int | ❌ | Tamaño mínimo en bytes |

**Ejemplo:**

```json
{
  "duplicates": [
    {
      "hash": "abc123...",
      "size": 123456,
      "files": [
        "/home/user/fotos/photo1.jpg",
        "/home/user/fotos/backup/photo1.jpg"
      ]
    }
  ]
}
```

### `find_patterns`

Detecta patrones en nombres de archivo (secuencias, prefijos, sufijos, fechas).

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Directorio a analizar |
| `strategy` | str | ❌ | auto, prefix, suffix, sequence, date |
| `min_group_size` | int | ❌ | Tamaño mínimo de grupo (default: 3) |

**Ejemplo:**

```json
{
  "patterns": [
    {
      "type": "sequence",
      "pattern": "menu{1}",
      "files": ["menu1.svg", "menu2.svg", "menu3.svg"]
    }
  ]
}
```

### `get_file_metadata`

Obtiene metadata detallada de un archivo específico.

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Ruta del archivo |

**Ejemplo:**

```json
{
  "name": "documento.pdf",
  "size": 45678,
  "modified": "2026-08-16T10:30:00Z",
  "created": "2026-08-16T10:30:00Z",
  "permissions": "rw-r--r--",
  "is_symlink": false
}
```

## Tools de caché

filegraph almacena resultados de escaneo en SQLite para que las búsquedas subsiguientes sean instantáneas.

### `index_directory`

Indexa un directorio y cachea sus resultados.

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Directorio a indexar |

### `sync_cache`

Sincroniza el caché — refresca archivos modificados, agrega nuevos.

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Directorio a sincronizar |

### `cache_status`

Muestra estadísticas del caché (entradas, archivos totales, tamaño).

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ❌ | Directorio a consultar (omitir = global) |

### `unindex_directory`

Elimina un directorio del caché.

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `path` | str | ✅ | Directorio a eliminar del caché |

## Comportamiento

- **Symlinks**: No se siguen por defecto (seguridad)
- **Permisos**: Si un directorio no tiene permisos de lectura, retorna resultados parciales con warning
- **Unicode**: Los nombres de archivos Unicode se preservan exactamente
- **Exclusiones por defecto**: `.git`, `node_modules`, `__pycache__`, `.venv` se excluyen automáticamente
- **Personalización**: Usa `--no-exclude` en CLI o `include_excluded=True` en tools para incluir directorios excluidos
- **Errores del filesystem**: Se informan con la ruta y el `errno`
  (`No such file or directory (errno 2): /ruta`), nunca como "no encontrado".
  `pathlib` devuelve `False` para EACCES/ESTALE/ENOTCONN/EIO, y ese silencio es
  lo que hacía que un mount caído se viera como un directorio vacío.

### Mounts de red

Un escaneo sobre un filesystem de red (CIFS/SMB, NFS, sshfs, 9p, AFS) es
**acotado por defecto**:

| Presupuesto | Valor por defecto |
|-------------|-------------------|
| `max_seconds` | `45` |
| `max_entries` | `200000` |

La raíz escaneada se clasifica leyendo `/proc/self/mountinfo` con un
longest-prefix match. Si no se puede determinar el filesystem (no-Linux, sin
mountinfo, error de parseo), se trata como **local**: un entorno desconocido
nunca se trunca en silencio.

En un filesystem local no cambia nada: `max_depth` sigue en `None`, sin deadline
y sin tope de entradas.

El deadline se cumple de verdad: el recorrido corre en un proceso hijo que se
mata al vencer el presupuesto, y las entradas descubiertas se streamean al padre.
Un chequeo cooperativo en Python no puede interrumpir un `scandir`/`stat`
bloqueado en el kernel.

El caché también se mueve: para una raíz de red la base vive fuera del share,
en `$XDG_CACHE_HOME/filegraph/<sha256 de la ruta resuelta>/cache.db` (o
`~/.cache/filegraph/…`), así que escanear un mount **no escribe nada** en él: ni
`.filegraph/`, ni `.db`, ni `-wal`/`-shm`. Un resultado truncado no se persiste,
y la verificación de frescura de un root de red usa el mismo presupuesto: si no
puede completarse, la entrada se considera vieja y se re-escanea (acotado). Una
raíz local mantiene el caché en `<ruta>/.filegraph/cache.db`, sin cambios.

## Árbol indexado (import / export)

Cuando el árbol es demasiado grande para escanearlo por el mount, `filegraph` lo **importa** de un
listado y consulta el índice local. El índice es un SQLite por raíz en
`$XDG_CACHE_HOME/filegraph/<sha256 de la raíz>/index.db`, fuera del árbol indexado.

```bash
# 1. listado generado donde viven los datos (una sola pasada de metadata)
find /ruta/al/arbol -printf '%y\t%s\t%T@\t%p\n' | gzip > arbol.tsv.gz

# 2. import (acepta archivo o stdin; re-importar reemplaza el índice)
filegraph import --root /ruta/local --format tsv <(zcat arbol.tsv.gz)

# 3. consultas: salen del índice, sin tocar el filesystem
filegraph export --root /ruta/local --format tsv
filegraph status --root /ruta/local
```

Un root indexado **nunca** se `stat`ea, ni se recorre, ni se escribe: las tools
(`scan_directory`, `search_by_type`, `find_patterns`, `find_duplicates`) y `cache_status` responden
desde el índice e informan `"source": "index"`. El TSV que exportás es re-importable tal cual.

Límites conocidos: `find_duplicates` sobre índice agrupa **por tamaño** (`hash: null`,
`content_hashed: false`) porque hashear exige leer contenido; los symlinks pierden el target (el TSV
de 4 columnas no lo trae); y `export` es un dump fiel, sin exclusiones por defecto.

### Ingesta por SSH

Si el árbol vive en un servidor y el mount local no sirve para transferir, `filegraph pull` trae el
listado por SSH y lo importa para la **ruta del mount local** que después consultás:

```bash
filegraph sources add nas \
  --host 10.0.0.114 --user filegraph --key /ruta/a/la/clave \
  --remote-path /mnt/pool/escaneos \
  --local-path /home/usuario/mnt/escaneos

filegraph pull --source nas            # stream find → import
filegraph sources list                 # nunca muestra material de la clave
```

Cada fila se mapea de `remote-path` a `local-path`; una fila fuera del prefijo remoto aborta el pull y
**deja intacto el índice anterior**, igual que un stream cortado.

**Modelo de seguridad.** No se usa shell en el servidor: la clave del cliente se restringe en
`authorized_keys` a un único comando de solo lectura.

```bash
#!/bin/sh
# /mnt/pool/scripts/filegraph-find  — único comando permitido a la clave
set -eu
case "${1:-}" in
  /mnt/*) ;;
  *) echo "path not allowed" >&2; exit 2 ;;
esac
exec find "$1" -printf '%y\t%s\t%T@\t%p\n'
```

```
# ~/.ssh/authorized_keys del usuario de solo lectura
restrict,command="/mnt/pool/scripts/filegraph-find" ssh-ed25519 AAAA… filegraph@pc
```

`restrict` desactiva pty, forwarding, agent y X11: esa clave solo puede ejecutar el wrapper. Si el
wrapper no acepta la ruta como argumento, fijá el dataset dentro del script y configurá la fuente con
`--remote-command` apuntando al wrapper. `filegraph` nunca lee ni imprime el contenido de la clave.

## CLI

El proyecto incluye un comando `filegraph` que se instala automáticamente:

```bash
# Ver ayuda
filegraph --help

# Comandos disponibles
filegraph scan <directorio>                    # Escanea y muestra árbol
filegraph search <directorio> <extensión>     # Busca por extensión
filegraph find-duplicates <directorio>        # Encuentra duplicados
filegraph find-patterns <directorio>          # Detecta patrones
filegraph index <directorio>                  # Indexa y cachea
filegraph sync <directorio>                   # Sincroniza caché
filegraph cache-status [directorio]           # Muestra stats del caché
```

### Ejemplos CLI

```bash
# Escaneo en formato JSON
filegraph scan /ruta/a/proyecto --format json

# Escaneo con profundidad limitada
filegraph scan /ruta/a/proyecto --format tree --depth 3

# Buscar duplicados con hash MD5 y tamaño mínimo
filegraph find-duplicates /ruta/a/proyecto --algo md5 --min-size 1000

# Buscar archivos .jpg incluyendo directorios excluidos
filegraph search /ruta/a/proyecto jpg --no-exclude

# Detectar solo secuencias con grupo mínimo de 5
filegraph find-patterns /ruta/a/proyecto --strategy sequence --min-group 5

# Indexar un directorio para búsquedas rápidas
filegraph index /ruta/a/proyecto

# Ver estado del caché
filegraph cache-status /ruta/a/proyecto
```

## Testing

```bash
python -m pytest tests/ -v
```

## Estructura del proyecto

```
filegraph/
├── pyproject.toml
├── .github/workflows/ci.yml
├── filegraph/
│   ├── __init__.py
│   ├── main.py           # Entry point del server MCP
│   ├── tools.py          # Definición de las tools MCP
│   ├── scanner.py        # Lógica core de escaneo
│   ├── store.py          # Persistencia SQLite del cache
│   ├── cache.py          # SQLite cache manager
│   └── cli.py            # Comando CLI filegraph
└── tests/
    ├── __init__.py
    ├── test_scanner.py
    ├── test_tools_cache.py
    ├── test_cli.py
    └── test_cli_cache.py
```
