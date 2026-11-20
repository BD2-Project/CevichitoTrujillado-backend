# Informe de integración de CevichitoTrujillano y SoupDB

La API de SoupDB-Copyright expone las seis operaciones que CevichitoTrujillano necesita para su flujo web actual. Las pruebas verificaron el cliente TCP v2 contra SoupDB real y el frontend en navegador con una API de prueba. Queda pendiente ejecutar el flujo completo con la API real y FMA Small. La compatibilidad de los cinco repositorios es parcial: rsoup v0.2.0 usa el protocolo legado, el análisis nativo de Cevichito sigue sin enlazar y la API mantiene el catálogo en solo lectura.

## Conexiones entre repositorios

| Conexión | Contrato y resultado |
| --- | --- |
| CevichitoTrujillano web → SoupDB-Copyright | HTTP REST bajo `/api/v1`. Nginx reenvía las peticiones al backend desde el mismo origen. El cliente mantiene soporte para un origen distinto mediante `VITE_URL_API`. |
| SoupDB-Copyright → SoupDB | Cliente Python propio sobre TCP v2, frame `0x02`. La integración conserva dependencias explícitas del esquema, consultas preparadas, el plugin HNSW y el volumen de audio. El backend comparte referencias de rutas y archivos de entrada con el motor. Sus operaciones sobre la base viajan por TCP. |
| rsoup v0.2.0 → SoupDB | El códec legado con frame `0x01` funcionó para consultas de tipos nativos, transacciones y sentencias preparadas en la prueba real. La compatibilidad v2 y los valores vectoriales en el protocolo requieren cambios. |
| SoupChef SSR → SoupDB | El servidor Node usa su propio códec y probó ambos frames con SoupDB real, incluidas consultas al catálogo del sistema, sentencias preparadas, transacciones y descarga de documentos con la raíz configurada. |
| SoupChef Tauri → SoupDB | Usa la revisión Git `ae3526b874eaa6524ac09188a6e56c870791e887` de rsoup. Se comprobó que existe en el remoto y que su códec pudo hacer `PING`, consultar `SysTables` y descargar bytes por `FETCH` contra SoupDB real. Falta compilar y probar la aplicación nativa completa. |
| CevichitoTrujillano Tauri → SoupDB | Cargo depende del directorio hermano `../../rsoup`. El comando `analizar_audio` devuelve un error explícito. El flujo de análisis necesita la API y el motor con su plugin. |

El flujo de demo es navegador → CevichitoTrujillano/Nginx → SoupDB-Copyright → SoupDB. Compose arranca esos tres servicios. SoupChef ofrece acceso directo opcional para inspeccionar la base.

## Rutas HTTP

| Método y ruta bajo `/api/v1` | Función y consumo |
| --- | --- |
| `GET /health` | Comprueba conectividad TCP con SoupDB. Compose lo usa para esperar al backend y la guía lo usa para verificar el proxy. |
| `POST /analyze` | Recibe el audio en el campo multipart `file`. El frontend presenta el resultado, usa `created_at` si viene en el POST y consulta el historial si falta. |
| `GET /catalog` | Lista obras y conteos de segmentos. El catálogo remoto declara `catalog_mutable: false`. |
| `GET /catalog/{song_id}` | Devuelve la referencia y sus segmentos indexados. |
| `GET /catalog/{song_id}/audio` | Sirve el audio original y admite solicitudes `Range`. El frontend usa el `audio_url` que publica el backend. |
| `GET /analyses` | Lista el historial persistido. |
| `GET /analyses/{analysis_id}` | Recupera un expediente guardado. |
| `GET /analyses/{analysis_id}/export?format=json\|csv` | Exportación adicional del backend. La interfaz genera sus archivos JSON, CSV y PDF en el navegador. |

El modo REST cubre análisis, consulta del catálogo, reproducción e historial. Las altas e importaciones existen en modo demo. Una versión con catálogo editable debe definir también cómo se incorporan los segmentos al HNSW. El contrato de catálogo ofrece identificación, título, artista, duración, conteo de segmentos y URL de audio. Para año, género, colección y licencia el frontend recurre a valores de presentación ante la ausencia de datos del backend, por lo que esos valores no acreditan metadatos de las obras. El POST entrega el resultado al finalizar. Los avances por etapa y los ajustes de análisis por solicitud requieren un contrato nuevo. La corrección del cliente trata un HTTP 413 del proxy como error de tamaño del archivo. Véanse el [registro de rutas](../src/copyright_app/api/app.py) y el [cliente REST](https://github.com/BD2-Project/CevichitoTrujillano/blob/cf56b86/src/lib/servicios/cliente-rest.ts).

## Brechas entre el motor y los clientes de escritorio

1. **Frame y encabezado de resultados.** SoupDB acepta el frame legado `0x01` y usa `0x02` para resultados v2. rsoup v0.2.0 y el pin de SoupChef usan el frame `0x01`. El encabezado v2 del motor codifica `size` como `u16`, mientras el decodificador moderno de rsoup espera `u32`. La prueba real confirmó el rechazo del frame y del resultado v2 por el códec de rsoup.
2. **Valores binarios y vectoriales.** El tag `0x06` representa `BYTES` con longitud `u32` en SoupDB y `VECTOR` con dimensión `u16` en rsoup. La prueba con un parámetro vectorial reprodujo el desacuerdo. La búsqueda de Copyright envía referencias de audio y el plugin calcula los vectores dentro del motor.
3. **Documentos multimedia.** La revisión fijada por SoupChef contiene `FETCH/DOCUMENT`. El rsoup v0.2.0 del repositorio local carece de esa operación. La descarga requiere iniciar SoupDB con `SOUPDB_DOCUMENT_ROOT` y guardar referencias relativas a esa raíz. El Compose de Copyright sirve el audio mediante HTTP desde su volumen de audio.
4. **Configuración de SoupChef.** La interfaz Tauri sigue la ruta a `127.0.0.1:55432` por defecto. Si se modifica el puerto publicado por Compose, se puede usar SoupChef SSR con `SOUPDB_HOST` y `SOUPDB_PORT` configurados. La indicación en Cargo de que v0.2.0 sustituye al pin quedó desactualizada. El estado de conexión de la UI Tauri está fijado y no constituye una comprobación de conectividad.
5. **TLS.** El cliente Rust ofrece TLS opcional. El servidor SoupDB de esta integración escucha TCP plano.

Referencias del contrato en [SoupDB](https://github.com/BD2-Project/SoupDB/blob/18ddd90/engine/transactions/protocol.py), [rsoup v0.2.0](https://github.com/BD2-Project/rsoup/blob/v0.2.0/src/protocol.rs), [rsoup fijado por SoupChef](https://github.com/BD2-Project/rsoup/blob/ae3526b874eaa6524ac09188a6e56c870791e887/src/protocol.rs) y [SoupChef](https://github.com/BD2-Project/SoupChef/blob/main/src-tauri/Cargo.toml).

## Cambios y evidencia

| Repositorio | Resultado de la revisión |
| --- | --- |
| SoupDB | 2.424 pruebas pasaron, una fue omitida y Ruff pasó. La revisión independiente comparó el AST Python antes y después de la limpieza excluyendo docstrings y comprobó los 350 archivos Python versionados. Los bloques consecutivos de comentarios de más de dos líneas se acortaron. Los docstrings que forman parte de la ayuda de CLI y de la documentación de API conservaron su función. La limpieza se publicó en `main` hasta `18ddd90`. |
| SoupDB-Copyright | 57 pruebas de TCP y desacoplamiento pasaron. El cliente v2 probó parámetros, transacciones y HNSW contra SoupDB real. La búsqueda sobre 12 segmentos sintéticos devolvió la coincidencia esperada antes y después de reiniciar. La revisión independiente confirmó el AST de los cambios de comentarios y la configuración Compose. Se corrigió el contexto de SoupDB y se incluyó `server` en la imagen porque el arranque importa `server.documents`. La imagen compensa que el wheel actual empaquete solo `engine`. FastAPI y Pydantic no estaban disponibles y el entorno restringió la instalación, así que quedó pendiente la prueba HTTP real. Los cambios previos a este informe se publicaron en `main` hasta `70b0e65`. |
| CevichitoTrujillano | Pasaron la comprobación de tipos, el build de Vite y 24 verificaciones de navegador contra una API de prueba, sin errores registrados en el navegador. Se revisaron audio, carga multipart, historial, errores y exportaciones. Se acortaron comentarios y se incorporaron el uso de `audio_url`, la fecha devuelta por el POST, el diagnóstico HTTP 413 y el proxy de la demo. Los cambios se publicaron en `main` hasta `cf56b86`. |
| rsoup | Pasaron 21 pruebas del códec con `rustc` y se probó el protocolo legado contra el motor real. La suite completa de Cargo quedó pendiente por dependencias inaccesibles. |
| SoupChef | Pasaron pruebas directas del transporte SSR y del códec fijado para Tauri. El build completo de SSR y la compilación Tauri quedaron pendientes por las dependencias y restricciones del entorno. |

El remoto de SoupDB-Copyright aceptó el push e informó que el repositorio se movió a [CevichitoTrujillado-backend](https://github.com/BD2-Project/CevichitoTrujillado-backend). La carpeta local de la guía sigue llamándose `SoupDB-Copyright`.

El build de Cevichito usa `npm ci` y el `package-lock.json` versionado. Los dos archivos locales de pnpm, que ya estaban presentes, quedaron fuera de los commits. En SoupDB quedaron dos directorios de resultados de pytest sin seguimiento. La política del entorno impidió eliminarlos.

## Demo local

1. Coloca `SoupDB`, `SoupDB-Copyright` y `CevichitoTrujillano` en carpetas hermanas. Inicia Docker para contenedores Linux y verifica que Compose utilice Buildx. En `SoupDB-Copyright`, copia `.env.example` a `.env` y apunta `FMA_AUDIO_ROOT` a la carpeta extraída de FMA Small que contiene `000/`, `001/` y las demás carpetas de audio.
2. Valida los perfiles y construye los servicios. Prepara y espera el catálogo antes de levantar la interfaz.

   ```sh
   docker compose --profile bootstrap --profile demo config --quiet
   docker compose --profile demo build
   docker compose --profile bootstrap up -d bootstrap-catalog
   docker compose --profile bootstrap wait bootstrap-catalog
   docker compose --profile bootstrap logs bootstrap-manifest codebook bootstrap-catalog
   ```

3. Confirma que el bootstrap terminó con código 0, con 80 canciones, 480 segmentos y el índice HNSW. Ejecuta `docker compose --profile demo up -d --wait cevichito`. Consulta `http://127.0.0.1:5173/api/v1/health` y `/api/v1/catalog` en el mismo origen. Abre `http://127.0.0.1:5173`, reproduce una referencia, sube una pista del catálogo, abre el resultado desde el historial y descarga un reporte. Los puertos corresponden a los valores de `.env.example`.
4. Si quieres mostrar el motor con SoupChef en paralelo, conéctalo a `127.0.0.1:55432` con la publicación por defecto y consulta `SELECT * FROM songs LIMIT 5` o `SELECT * FROM SysPluginIndexes`. El modo web de SoupChef en el host requiere configurar su host de SoupDB como `127.0.0.1`.
5. Detén los servicios con `docker compose --profile demo --profile bootstrap down`. Un apagado ordenado conserva el catálogo y el historial en los volúmenes de Compose.

Si falla el bootstrap, revisa los logs del manifest, el codebook y el catálogo, y confirma la ruta FMA y los contextos de build. Si el frontend devuelve 502 o 503, revisa `docker compose --profile demo logs soupdb copyright-api cevichito` y verifica la salud de la API directa en el puerto 8000. Para Vite desde el host, configura `COPYRIGHT_CORS_ORIGINS` con el origen exacto que abras en el navegador. Los [pasos y variables](../README.md#demo-local-con-docker) incluyen la alternativa sin frontend en contenedor.

Compose validó ambos perfiles. En la ejecución con Buildx disponible, Compose seleccionó las tres imágenes y el build se detuvo porque el daemon de contenedores Linux de Docker Desktop no estaba disponible. El dataset FMA Small tampoco estaba disponible para la prueba completa. La verificación pendiente para dar por cerrada la demo es construir las tres imágenes, completar el bootstrap con FMA y repetir el recorrido anterior contra la API real. Si el alcance incluye análisis nativo, edición del catálogo o progreso en vivo, esos contratos deben implementarse y verificarse por separado. Para ampliar la compatibilidad de rsoup, primero hay que unificar la versión, el encabezado y los tags con pruebas de interoperabilidad contra el motor.
