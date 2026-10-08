# Detector CALL/PUT y ejecucion demo

Windows, Python 3.14, Microsoft Edge y las dependencias de
[requirements.txt](requirements.txt).

```powershell
Set-Location 'D:\07 - Script'
python -m pip install -r requirements.txt
python bot.py
```

Si Python no esta en PATH, usa la ruta completa de tu interprete.

## Detectores OpenCV y pruebas offline

[candle_tracker.py](candle_tracker.py) usa OpenCV/HSV para probar por separado
la linea roja vertical de expiracion, la ultima vela de una serie regular
y un punto blanco compacto cerca de ella. No importa `bot.py`, no abre el
navegador y no envia operaciones. El bot utiliza estos detectores mediante
[live_candle_reference.py](live_candle_reference.py), con una validacion
estricta adicional para permitir el reconocimiento de flechas.
Las dependencias estan en [requirements.txt](requirements.txt);
[requirements_tracker.txt](requirements_tracker.txt) incluye ese mismo archivo.

```powershell
Set-Location 'D:\07 - Script'
python -m pip install -r requirements_tracker.txt
python test_candle_tracker.py --image 'C:\capturas\grafico.png'
python test_expiration_line.py --image 'C:\capturas\grafico.png'
python test_candle_detector.py --image 'C:\capturas\grafico.png'
python test_white_point.py --image 'C:\capturas\grafico.png'
```

Cada comando genera `<nombre>.<componente>.debug.png` y un JSON con coordenadas,
confianzas, estados y motivos de deteccion incompleta. `--output` cambia la
ruta del PNG. No se sobrescriben las capturas fuente. Los bounding boxes son
`[x1,y1,x2,y2]`, con borde derecho/inferior exclusivo, en pixeles de la imagen
original. La imagen de debug conserva el grafico a su resolucion y agrega
un panel inferior: magenta=expiracion, cian=bounding box, amarillo=cuerpo,
azul=mecha superior, naranja=mecha inferior y circulo blanco=punto.
Una mecha no visible se informa como `null`, no se inventa.
Codigo de salida: 0=deteccion solicitada encontrada, 2=incompleta con debug
disponible, 1=error de entrada/archivo. Los comandos por componente comparten
la extraccion de contexto espacial, pero verifican su objeto correspondiente.

La ROI por defecto excluye el 1% izquierdo, 3% derecho, 14% superior y 8%
inferior del perfil de grafico mostrado en la captura de referencia. No
son coordenadas del punto ni de la vela. Para capturas con navegador/paneles
distintos es necesario indicar el area valida: `--roi 'x1,y1,x2,y2'`.
No hay reconocimiento universal de botones, textos ni disenos de plataforma.

`WhitePointTracker` guarda x, y, radio, confidence y timestamp. Combina brillo,
baja saturacion, tamano, circularidad, compacidad, proximidad a la vela y
soporte horizontal de precio (puede estar visible solo a un lado).
El punto no tiene que estar exactamente en el centro del cuerpo.
Tras adquirirlo, busca localmente en +/-24 px X y +/-40 px Y; si falla
amplia tres veces esos radios, siempre dentro de la ROI y cerca de la vela.
Si falta o es ambiguo, devuelve `null`, marca `LOW_CONFIDENCE` y solicita
recalibracion. En el siguiente frame vuelve a adquirirlo cerca de una vela
detectada; no devuelve la coordenada vieja como deteccion actual.
Cambiar ROI/resolucion o superar un segundo sin punto reinicia la continuidad.
Los timestamps deben aumentar; de otro modo se informa un error.

Para comparar capturas consecutivas:

```powershell
python test_candle_tracker.py --image 'C:\capturas\frame1.png' `
  --next-image 'C:\capturas\frame2.png' --next-image 'C:\capturas\frame3.png' `
  --frame-interval 0.2 --output 'C:\capturas\secuencia.debug.png'
```

El JSON incluye todos los frames y el PNG muestra el ultimo. El intervalo
debe coincidir con el tiempo real entre capturas; de ello depende velocity_y
(px/s). Delta Y negativo=UP, positivo=DOWN, dentro de +/-1 px=STABLE.
La primera captura y las readquisiciones no tienen movimiento medido.
Estos datos no generan senales de trading.

La confianza global pondera vela 40%, punto 35%, expiracion 25% y agrega
3 puntos porcentuales si coinciden espacialmente. Objetos ausentes aportan
cero y sin coincidencia completa el score no supera 69%. Las detecciones
parciales se conservan para debug; no autorizan entradas.
En vivo se exigen los tres objetos actuales y coincidencia espacial,
confidence global >=80%, vela >=70%, punto >=65% y linea >=70%.
Los scores son heuristicas de calidad visual, **no probabilidades calibradas**
ni garantia de identificar la vela en tiempo real. La linea se identifica
por color/forma, sin leer su etiqueta; otros disenos requieren validacion.
Las superposiciones de precio se cierran morfologicamente para reconstruir
continuidad del cuerpo/mecha; sus limites visibles pueden variar unos pixeles.

Pruebas sinteticas independientes:

```powershell
python -m unittest test_expiration_line test_candle_detector test_white_point test_candle_tracker
```

La regresion de la captura compartida de 1526x768 se activa definiendo
`IQ_OPTION_REFERENCE_IMAGE` con su ruta antes de ejecutar ese comando.
Comprueba linea, cuerpo, bounding box, ambas mechas, color, punto y scores
a escalas 0.75, 1 y 1.5. Esa variable debe apuntar a **esa misma referencia**,
no a otra captura. Una imagen real no verifica tracking temporal; las pruebas
de desplazamiento, perdida y readquisicion usan secuencias sinteticas.

### Dos posiciones de la vela respecto a la linea roja

El detector contempla tanto el cuerpo a la izquierda de la
linea roja como el cuerpo atravesado por esa linea (captura de apertura).
En el segundo caso, el punto puede estar a la derecha de la linea sin ser
rechazado: debe seguir cerca del centro y del rango vertical de la vela.
No se infiere la apertura ni el tiempo transcurrido solo por esta posicion.
Se conserva la comprobacion de forma y espaciado de la serie; no se acepta
arbitrariamente cualquier cuerpo situado a la derecha de la linea.

Para separar una vela roja de la linea roja que la atraviesa, se elimina
una franja estrecha alrededor de la linea detectada antes de segmentar los
cuerpos y se reconstruye ese hueco horizontal. Asi la linea no se mide como
una mecha larga. Los extremos ocultos por la linea o el punto pueden ser
inciertos; las mechas no visibles siguen siendo `null`.
La imagen de debug indica `LEFT OF RED LINE` o `CROSSES RED LINE`.
La regresion de la segunda captura compartida (1063x768) se activa con
`IQ_OPTION_OPENING_IMAGE`. Puede ejecutarse junto con la primera referencia
para verificar ambas a escalas 0.75, 1 y 1.5. Esta captura ya esta recortada
al grafico: opcionalmente usa `--roi '0,0,1063,750'` para incluir la parte
superior sin el icono inferior.

## Uso

### Analisis visual de EMA y filtro de entradas

[ema_analyzer.py](ema_analyzer.py) reconoce las curvas que ya dibuja la
plataforma: EMA 9 cian/azul y EMA 21 amarillo/naranja. No calcula medias
desde precios ni genera ordenes por si solo. Tras verificarlo en tiempo real,
se implemento un filtro opcional de las flechas existentes. Actualmente esta
**desactivado** (`EMA_ENTRY_FILTER_ENABLED = False`): las EMA se muestran
solo como informacion y nunca bloquean ni autorizan entradas, incluso en
SIDEWAYS, CROSSING, UNKNOWN, baja confianza o perdida de deteccion.
Se mantienen todos los requisitos de flecha, vela, punto, linea y cuenta DEMO.
Las reglas siguientes solo aplican si se habilita explicitamente el filtro:

- CALL requiere EMA `BULLISH` con confianza >= `EMA_MIN_CONFIDENCE`.
- PUT requiere EMA `BEARISH` con esa misma confianza minima.
- `CROSSING`, `SIDEWAYS`, `UNKNOWN`, errores, ausencia o datos desactualizados
  bloquean entradas. No se opera exclusivamente por cruces EMA.

Se acepta una sola captura valida de flecha, con rearme por dos ausencias,
referencia de vela/punto/linea, una senal por intervalo y protecciones DEMO.
Cada captura que cuenta para validar la flecha debe superar el filtro EMA;
un bloqueo reinicia ese conteo, pero NO cuenta como ausencia para rearmar.
Si las EMA se recuperan y la flecha sigue presente, basta una captura
compatible, siempre que ya hubiera rearme valido.
Antes de `submit` se toma una captura nueva y se revalidan referencia,
flecha y EMA. Si el filtro falla entonces, no se envia solicitud y no se
reintenta esa senal en el mismo intervalo. El detector y DEMO siguen activos.
Los bloqueos EMA no suman Intentos (reservado a vencimientos de ejecucion).
La confianza visual no garantiza rentabilidad. No se modifico el mecanismo
web que envia el clic; las EMA se verifican antes de entrar en su preparacion,
no de forma continua mientras el navegador prepara la orden.

Primero se probo el modulo independiente con la captura compartida de
1000x289: EMA 9 encima de EMA 21 al extremo derecho, ambas pendientes
positivas, distancia aproximada 68px, BULLISH, confianza aproximada 92%.
Se genero `captura_ema.debug.png` y su JSON; se verificaron tambien escalas
0.75, 1 y 1.5. La captura contiene un cruce historico visible, pero una sola
imagen no confirma un cruce temporal nuevo.

```powershell
python test_ema_analyzer.py --image "C:\ruta\captura.png" --output "captura_ema.debug.png"
```

Genera PNG anotado y JSON con trayectoria, medidas, factores de confianza,
estado, timestamp y evento de cruce. No altera la captura original.
Si la captura contiene interfaz, selecciona el grafico con
`--roi "x1,y1,x2,y2"`. Sin ROI se analiza la imagen completa; la captura
compartida ya contiene solo grafico. Una curva extensa de igual color de
otro indicador no puede identificarse como EMA por color solamente:
otras curvas comparables del mismo color se rechazan como ambiguas.
Evita incluir leyendas, controles o multiples graficos en la ROI.

Para probar seguimiento/cruces necesitas imagenes consecutivas, en orden:

```powershell
python test_ema_analyzer.py --image "frame1.png" --next-image "frame2.png" --next-image "frame3.png" --frame-interval 0.2
python -m unittest test_ema_analyzer test_live_candle_reference
```

El intervalo representa el tiempo real entre capturas; no reproduce tiempo
real ni inventa movimiento de una unica imagen. UNKNOWN sale con codigo 2
y motivo explicito; errores de archivo/configuracion con codigo 1.
Los tests de la captura compartida se activan con `IQ_OPTION_EMA_IMAGE`.

La segmentacion HSV usa mascaras independientes y cierre morfologico 3x3
para conservar lineas de un pixel. Descarta componentes cortos, gruesos,
verticales o trayectorias ambiguas; une segmentos compatibles y solo
interpola huecos pequenos. No extrapola una curva hacia columnas sin evidencia.
Se mide la relacion en las ultimas cinco columnas comunes; `analysis_x`
indica el extremo analizado, no una coordenada fija ni un precio.
Los extremos de ambas curvas deben coincidir dentro de la tolerancia de hueco.

Y menor significa precio visual mayor: `ABOVE` es EMA 9 por encima.
La pendiente numerica es el negativo de la regresion de Y respecto a X
sobre una ventana reciente: POSITIVE significa ascendente en precio visual.
Se expresa en px verticales por px horizontal, no en unidades monetarias.

Los ajustes documentados estan en [config.py](config.py):

| Ajuste | Default | Interpretacion |
|---|---|---|
| `EMA_FAST_HSV_LOW/HIGH` | (85,140,140)/(115,255,255) | Cian/azul; H OpenCV 0..179 |
| `EMA_SLOW_HSV_LOW/HIGH` | (10,140,140)/(38,255,255) | Amarillo/naranja saturado |
| `EMA_SLOPE_LOOKBACK` | 100 | Columnas comunes recientes para regresion |
| `EMA_SLOPE_FLAT_MARGIN` | 0.03 | Banda neutra en px/px |
| `EMA_RELATION_MARGIN_PX` | 2 | Diferencias menores se consideran TOUCHING |
| `EMA_CLOSE_DISTANCE_PX` | 4 | CLOSE hasta 4px, inclusive |
| `EMA_SEPARATED_DISTANCE_PX` | 12 | SEPARATED desde 12px; intermedio NORMAL |
| `EMA_DISTANCE_CHANGE_MARGIN_PX` | 1 | Cambio minimo entre frames para expansion/contraccion |
| `EMA_MIN_POINTS` | 80 | Columnas observadas minimas por curva y comunes |
| `EMA_MIN_SEGMENT_WIDTH` | 20 | Ancho minimo de componente |
| `EMA_MAX_LINE_THICKNESS_PX` | 6 | Limite de grosor para excluir bloques/velas |
| `EMA_MAX_GAP_PX` | 28 | Hueco maximo interpolado; captura: 14px nativa, 25px a 1.5x |
| `EMA_MAX_STEP_SLOPE` | 2 | Maximo cambio local vertical/horizontal |
| `EMA_TRACK_RADIUS_PX` | 12 | Busqueda alrededor de trayectoria previa |
| `EMA_RECOVERY_RADIUS_PX` | 36 | Busqueda ampliada si falla seguimiento |
| `EMA_RECALIBRATION_FRAMES` | 3 | Perdidas antes de volver a busqueda completa |
| `EMA_MAX_FRAME_GAP_SECONDS` | 1 | Reinicia seguimiento tras discontinuidad temporal |
| `EMA_CROSS_CONFIRMATION_FRAMES` | 3 | Frames para establecer cada lado del cruce |
| `EMA_MIN_CONFIDENCE` | 75 | Minimo 0..100; por debajo, UNKNOWN |
| `EMA_ENTRY_FILTER_ENABLED` | False | Opcional: True exige BULLISH para CALL o BEARISH para PUT |
| `EMA_CONFIDENCE_WEIGHTS` | .20,.25,.20,.15,.20 | Color, continuidad, puntos, temporal, relacion |

Estos son umbrales visuales iniciales para la captura proporcionada, no
valores universales ni recomendaciones de trading. Distancias/grosor/huecos
son pixeles: revisalos al cambiar zoom, resolucion, colores o DPI.
El limite de hueco se ajusto a oclusiones medidas, no para unir tramos
arbitrariamente separados. La confianza es calidad visual, no probabilidad
de acertar una operacion. Sin historial, el factor temporal vale 0.5;
no se presenta una captura como seguimiento confirmado.

Estados: BULLISH exige ABOVE y ambas pendientes POSITIVE sin contraccion
significativa; BEARISH exige BELOW y ambas NEGATIVE sin contraccion.
TOUCHING o una inversion pendiente/confirmada producen CROSSING, salvo
lineas cercanas y planas (SIDEWAYS). Si las direcciones no apoyan una tendencia
conjunta, se clasifica SIDEWAYS. Falta/ambiguedad/baja confianza produce UNKNOWN.
`distance_increasing` es null en el primer frame.

Un cruce temporal requiere establecer un lado durante tres frames y luego
el otro lado durante tres frames fiables consecutivos. CALL/PUT en
`crossover_detected` solo describe la direccion del cruce, no una orden.
Se emite una vez al confirmar la inversion, no mientras continua separandose.
`previous_relation` conserva el lado previamente confirmado;
`crossover_timestamp` usa segundos monotonos (en CLI tiempo simulado).
Una perdida rompe la persistencia y no permite inferir un cruce a traves
de frames ausentes. `spatial_crossing` solo indica interseccion historica
en la trayectoria visible, y no dispara ese evento temporal.

En vivo [live_ema_analysis.py](live_ema_analysis.py) adapta screenshots PIL,
conserva el seguimiento y entrega `App.ema_result`. El panel muestra medidas,
estado, confianza, busqueda y errores aunque no pueda confirmarse una vela.
Con `DEBUG_MODE = True` se dibujan curvas, puntos y etiquetas en la vista
previa; con False permanece el panel sin overlay.
`EMA_ENTRY_FILTER_ENABLED = False` y reiniciar vuelve al analisis informativo
y al criterio de entradas anterior. Desactivar el analisis tambien requiere
desactivar explicitamente ese filtro: INICIAR rechaza un filtro habilitado
con `EMA_ANALYSIS_ENABLED = False`, para no operar sin evidencia.
INICIAR/DETENER, cambio de tamano/ROI o hueco temporal reinician seguimiento.
Las coordenadas retenidas solo guian la busqueda: no se publican como deteccion
actual cuando se pierden las lineas.

1. El bot abre una ventana dedicada de Microsoft Edge con su perfil local
   anterior. Inicia sesion manualmente si hace falta; no crea una cuenta.
   No utiliza el navegador predeterminado ni tu perfil habitual.
2. Selecciona **Cuenta demo**, en dolares, interfaz en espanol e instrumento
   **Binaria**. Configura el importe y la temporalidad del grafico en **1m**
   o **5m**. En el selector del bot elige **60** (1 minuto) o **300**
   (5 minutos), coincidiendo con el grafico. El valor inicial sigue siendo 300.
   Para cambiar temporalidad, detiene el bot, cambia el grafico y el selector,
   recalibra las muestras/zona si cambia su aspecto y pulsa INICIAR.
3. Carga [el indicador QCS](ema_rsi_iq_option.lua), calibra las dos muestras
   de flechas y selecciona SOLO el grafico en tiempo real: incluye al menos
   tres velas completas, espacio de flechas, punto blanco y linea roja.
   Excluye botones, textos de la interfaz y paneles laterales.
4. Pulsa **INICIAR detector y operaciones DEMO** y confirma el inicio.
   Valida la cuenta demo y configuracion del grafico antes de habilitar
   detector y entradas; ya no hay un boton ARMAR separado. Si falla,
   no inicia. No consulta ni recarga la cartera al iniciar. No se activa
   automaticamente al abrir el programa.
5. Verifica que la franja blanca siga la ultima vela. Las flechas presentes
   al iniciar no generan entradas retroactivas; espera una senal nueva.

6. CALL pulsa **Sube**; PUT pulsa **Baja**. PUT no cierra una posicion existente.
7. **PARADA DE EMERGENCIA**, DETENER o Esc desarman. No cancelan posiciones
   que ya se hayan enviado.

No hagas operaciones manuales, recargas, cambios de cuenta ni cambios del
grafico mientras este armado. Un cambio detectado de importe, activo o
temporalidad bloquea la ejecucion.
Si se detecta una configuracion modificada antes de entrar, el error indica
el activo, importe o temporalidad que difiere, con sus valores anterior y
actual. El vencimiento que avanza automaticamente no cuenta como ese cambio.

### Vencimiento al cierre de la vela de 1 o 5 minutos

Ante una nueva senal validada, el bot ajusta **Tiempo** al siguiente limite
:00, :05, :10, etc. en modo 5m, o al siguiente minuto en modo 1m,
calculado en hora de Colombia (UTC-5) desde el reloj
del PC. Por ejemplo, una senal a las 00:16:30 vence a las 00:20:00,
no a las 00:21:30. En modo 1m, una senal a las 00:16:30 vence a las
00:17:00. No garantiza una duracion completa desde la senal.
Sincroniza el reloj del PC y configura la web en hora de Colombia.

El avance automatico del campo Tiempo no obliga a rearmar: antes de cada
entrada se selecciona y verifica el cierre correspondiente. Si no aparece
esa hora en el selector, cambia la vela durante el ajuste, la senal caduca
o quedan dos segundos o menos para el cierre, se bloquea la entrada.
Nunca se sustituye por el vencimiento de otra vela.

## Registro

### Varios monitores

Mueve primero el navegador a la pantalla donde operaras. Elige esa pantalla
en el selector del bot y luego calibra CALL, PUT y la zona. El selector
aparece en el monitor elegido; admite pantallas a la izquierda o arriba
del principal (coordenadas negativas) y captura solo la zona seleccionada.
Puedes colocar el panel del bot en otra pantalla.
La vista previa muestra cada nueva captura del grafico durante el
seguimiento normal (intervalo de 100 ms mas el tiempo de procesamiento),
sin parpadeo. El marco blanco sigue la vela actual identificada.
Las lineas se dibujan despues de reducir la vista para conservar su grosor.
La vista anota linea de expiracion (magenta), bounding box (cian), cuerpo
(amarillo), mechas (azul/naranja) y punto (circulo blanco). La franja blanca
se centra en la vela, con tolerancia maxima de 3 pixeles para las flechas;
un punto descentrado no desplaza esa franja.
El panel muestra confianza visual, color, coordenadas y movimiento del punto.
Se usa toda la zona manual seleccionada como ROI, sin recortar porcentajes
de encabezado/pie como en las pruebas offline.
Los inputs de colores del bot siguen definiendo los colores de los cuerpos
y mechas (tolerancia 24 por canal); linea y punto usan filtros HSV.
No se exige movimiento. La referencia necesita los tres objetos visibles,
su coincidencia espacial y los umbrales de confianza indicados arriba.
Ante perdida de cualquiera, se muestran las detecciones parciales pero
se bloquean flechas/entradas y se borra la validacion y el rearme de senal.
El detector y la ejecucion DEMO permanecen activos, buscando otra referencia
valida; no se sustituye un punto ausente por su coordenada anterior.
El tracker de punto busca localmente y amplia si falla; vuelve a adquirirlo
si hace falta. Al cambiar de intervalo se reinicia el tracker para no
heredar el punto de la vela anterior. INICIAR y DETENER tambien lo reinician.
Un cuerpo de menos de tres filas coloreadas aun no se confirma: no se
autoriza una entrada sobre una vela apenas visible o sin cuerpo medible.
Una flecha CALL/PUT nueva dentro de la franja actual puede habilitar una
entrada aunque el cuerpo de la vela este quieto. Se mantienen dos capturas
sin flecha para rearmar, una captura valida de reconocimiento y
como maximo una senal por intervalo. Las flechas presentes al iniciar y
las centradas fuera de la referencia no habilitan entradas. Un desplazamiento
de la vela mayor de 3 pixeles reinicia el rearme; antes de solicitar la orden
se toma otra captura y se exige de nuevo linea, vela, punto, coincidencia
y confianza, con centro de vela a no mas de 2 pixeles del anterior y la misma
direccion de flecha. No exige la misma coordenada Y del punto: puede moverse
con el precio. Ante perdida de referencia siempre se vuelve a exigir dos
capturas validas sin flecha y un reconocimiento valido.
Se conserva el contador Intentos y la continuidad al vencer el tiempo de
entrada; no se convierten fallos visuales en intentos por tiempo.
La comprobacion es visual, no prueba la hora de la vela ni garantiza ausencia
de falsos positivos. Los limites de tamano se deben verificar con el zoom
real; no se relajan automaticamente tras errores. Se conservan las comprobaciones
de cuenta DEMO, configuracion, vencimiento y solicitud pendiente.

Cambiar de monitor detiene y desarma el bot y borra las muestras y la zona.
Si desconectas pantallas o cambias su distribucion/resolucion, pulsa
**Actualizar monitores** y recalibra. Se usa conciencia DPI por monitor
para evitar virtualizacion de coordenadas. Si cambia el tamano de las flechas
por escalado, necesitas nuevas muestras.

No sigue automaticamente el navegador entre pantallas. Si lo mueves o
redimensionas despues de calibrar, detiene primero el detector y recalibra.

La base local `operaciones_demo.sqlite3` conserva solicitudes y posiciones
confirmadas por su ID. `senales_simuladas.csv` sigue siendo solamente el
registro de detecciones y no confirma que haya habido una operacion.

El panel presenta solo las estadisticas generales desde el ultimo reinicio:

- Ganadas/Perdidas: cantidad de operaciones cerradas cuyo importe devuelto
  fue mayor/menor que la inversion. Un empate no cuenta como ninguna.
- Neto invertido: suma de las inversiones de operaciones cerradas.
- Ingresos: suma de importes devueltos en operaciones cerradas, leidos del
  campo **Rendimiento** de los detalles observados.
- Perdidas: monto no recuperado en las operaciones con devolucion menor que
  su inversion.
- Intentos: entradas canceladas exclusivamente por tiempo vencido (senal
  mayor de 6 segundos, vela cerrada o 2 segundos o menos para su cierre).
  No incluye operaciones enviadas ni otros bloqueos. Se guarda en el registro
  local y se pone a cero con **REINICIAR ESTADISTICAS**, sin borrar su historial.

Si vence el tiempo antes del clic de orden, esa entrada se omite y el bot
permanece activo y armado, esperando una nueva senal validada. No reintenta
la senal vencida en el mismo intervalo. No cuenta como perdida ni inversion.
Los errores de cuenta, configuracion, navegador y clic incierto mantienen
sus bloqueos de seguridad y no se convierten en intentos por tiempo.

El boton **REINICIAR ESTADISTICAS** confirma antes de poner el corte
estadistico en la hora actual. No borra operaciones de `operaciones_demo.sqlite3`
ni operaciones abiertas/inciertas; el historial permanece para exportacion
posterior. Tampoco reinicia el limite de tres perdidas de la activacion ni
arma/desarma el bot. El saldo de cuenta no se contabiliza.
El panel tambien muestra por separado **Perdidas de seguridad: n/3**.
Este contador se reinicia al volver a armar la ejecucion, no al reiniciar
las estadisticas visibles.

El saldo de la cuenta y las operaciones previas al armado no se contabilizan.
Los datos no interpretables no se sustituyen por cero. No se calcula una
ganancia a partir del porcentaje de rentabilidad anunciado.

Una sola solicitud pendiente bloquea nuevas compras. No hay limite de
solicitudes por activacion; se mantiene una solicitud por activo e intervalo
inferido del reloj del PC, conservada incluso tras reiniciar.

La ejecucion se desarma al confirmar **3 perdidas totales por activacion**,
sumando CALL y PUT aunque haya ganancias intermedias. Una perdida es una
operacion cerrada cuya devolucion es menor que su inversion, incluso si
devuelve parte del importe. Empates, operaciones pendientes o resultados
inciertos no cuentan. El detector sigue mostrando senales, pero no envia
mas ordenes. Volver a armar manualmente reinicia el contador; las perdidas
historicas no cuentan para la nueva activacion. Para reactivar usa INICIAR
y confirma de nuevo; DETENER/Esc sigue deshabilitando las entradas.

Si un clic falla o no se encuentra una posicion nueva unica, su estado
queda pendiente/incierto: **no se reintenta**. Consulta los resultados
despues de revisar la cartera; si el sitio no permite conciliar el ID, el
registro seguira bloqueado. No borres la base para forzar nuevos clics.

Solo despues del vencimiento de una solicitud del bot se consulta
automaticamente la cartera: no hay boton de consulta ni paso manual.
Al enviar la solicitud se programa la validacion para el vencimiento;
si el resultado sigue pendiente, se vuelve a consultar cada cinco segundos.
La hora de
vencimiento queda guardada para conservar esa espera tras reiniciar.
Se asocia una operacion por direccion, activo, importe y hora de apertura
(desde dos segundos antes hasta quince segundos despues de la solicitud,
sin superar el vencimiento), interpretada en hora de Colombia. Varias
coincidencias o una hora ilegible bloquean la conciliacion; no se supone
que una operacion historica sea la nueva. No hagas operaciones manuales:
ya no se comprueban posiciones manuales abiertas antes de iniciar.

La consulta de resultados usa otra pestana del mismo perfil, la trae al
frente durante la lectura y vuelve al grafico al terminar, incluso si
la lectura falla. Espera hasta 15 segundos por elemento de la cartera
(secciones Activas/Cerradas, tarjetas y detalles), sin ampliar el tiempo
de espera de los clics de compra. Durante esa lectura la interfaz del bot
puede no responder inmediatamente. Un error de carga bloquea el armado;
comprueba la sesion y el historial visible antes de reintentar.
No cierres la pestana de cartera durante esa comprobacion: interrumpe
la validacion y bloquea las entradas. Si estaba cerrada antes de consultar,
el bot la crea de nuevo. Si la cierras durante la consulta, muestra el
motivo y vuelve a intentar leer el resultado automaticamente, sin repetir
el clic de compra/venta ni reactivar las entradas por si solo. Las operaciones
historicas nunca generan senales.

El minimo para reconocer CALL y PUT es **17x24 pixeles**, inclusive:
ancho >=17 y alto >=24 de la silueta coloreada visible, no del recuadro seleccionado.
Por debajo de cualquiera de esos limites la figura se rechaza.
Se permite reducir la silueta calibrada en ancho y alto hasta ese minimo,
conservando la comparacion de forma y color y la coincidencia >=70%.
Por ejemplo, una muestra de 23x27 puede reconocer 17x24, 19x24 o 20x24;
las dimensiones por si solas nunca autorizan una entrada.
Se conservan los limites superiores de muestra +1px de ancho y +2px de alto:
un aumento mayor de zoom requiere recalibrar.
Si una linea superpuesta divide la flecha, se agrupan fragmentos del mismo
color separados por hasta 3 pixeles faltantes en horizontal o vertical
(`MAX_ARROW_FRAGMENT_GAP` en `bot.py`). Se incluyen piezas estrechas de al menos
3 pixeles coloreados y se verifica el centro del conjunto en la franja actual.
El conjunto no puede exceder los limites de la muestra y debe conservar
una coincidencia >=70%. No se rellenan huecos ni se inventan extremos:
una silueta visible de 23x23 sigue siendo rechazada por altura insuficiente.
Se usa la misma reconstruccion en la captura inicial y en la captura final.
Las flechas completas siguen reconociendose sin agruparse con ruido cercano;
varias flechas reconocidas siguen bloqueando por ambiguedad. Cortes mayores,
perdida excesiva de color o grupos con forma incompatible se rechazan.
Algunas flechas recortadas pueden superar el umbral de forma si aun cumplen
el minimo; este filtro no garantiza ausencia de falsos positivos.
Una figura rechazada del color esperado
no cuenta como captura sin flecha para rearmar: evita habilitar como nueva
una flecha que solo alterna entre reconocida y rechazada. Se acepta una
captura valida y se mantiene el bloqueo de flechas ya visibles al cambiar de vela.
El umbral del 70 % permite mayor variacion de forma, pero aumenta el riesgo
de aceptar figuras que no sean flechas respecto al umbral anterior del 88 %.
Las capturas usan pausas de 100 ms, mas el procesamiento. Tras el rearme,
la primera flecha reconocida habilita la comprobacion final, sin esperar
otras dos capturas de validacion. Se conserva la captura final de seguridad
antes de solicitar la orden. Esto admite senales fugaces que antes no
completaban tres capturas y aumenta el riesgo de entradas por repintado.
Las mascaras de flechas se
calculan con Pillow; OpenCV detecta linea, vela y punto para validar la referencia.
La preparacion reutiliza la configuracion recien leida por submit; si se
cambia Tiempo, vuelve a leerla despues del ajuste. Si Tiempo ya coincide
con el cierre, no se repite la lectura completa durante su preparacion.
Se conserva la lectura
inicial y la comprobacion completa inmediatamente antes del clic.
Esto reduce trabajo, pero no extiende el horario de compra que permite
IQ Option ni sustituye un vencimiento no disponible por otro.
El diagnostico mide por separado la lectura de configuracion, preparacion
de vencimiento y validacion final/clic. Distingue una senal de mas de 6
segundos de una vela que ya cerro. No cambia el momento en que el indicador
genera la flecha ni limita nuevas senales a los primeros segundos de la vela.
El limite de antiguedad es de 6 segundos al recibir la solicitud y al
terminar la preparacion; no introduce una espera obligatoria. Se conserva
el bloqueo cuando quedan dos segundos o menos para el cierre.
Tras reiniciar pulsa INICIAR: si existe una solicitud pendiente, las
entradas quedan bloqueadas y su validacion se programa automaticamente.
Una vez resuelta, vuelve a pulsar INICIAR para activar nuevas entradas.

## Pruebas desde VS Code

La configuracion de `.vscode/settings.json` habilita unittest y descubre
`test_*.py` en el panel Testing. Selecciona el interprete con Pillow,
Playwright, NumPy y OpenCV instalados, actualiza el descubrimiento y ejecuta las pruebas.
No requieren abrir IQ Option ni enviar ordenes. Tambien pueden ejecutarse
desde la terminal integrada:

```powershell
python -m unittest -q test_bot test_demo_execution
python -m unittest -q test_live_candle_reference test_expiration_line test_candle_detector test_white_point test_candle_tracker
```

Las regresiones cubren cuerpos diminutos, franjas de seguimiento,
reconocimiento de flechas sobre velas quietas, flechas fragmentadas CALL/PUT,
rechazo de texto/rectangulos/mechas y protecciones DEMO. La regresion opcional
`IQ_OPTION_FRAGMENT_IMAGE` usa el recorte compartido de 315x401: verifica
que sus fragmentos formen 23x23 sin autorizar una entrada bajo el minimo.

## Limites y privacidad

### Diagnostico de senales sin entrada

`diagnostico_bot.log`, junto al script, conserva el armado/desarmado,
las senales validadas, los intentos de entrada, los bloqueos por solicitud
pendiente y los errores completos. Se conserva al cerrar el bot y rota
al alcanzar 1 MB (hasta tres copias anteriores). Las fechas del registro
usan la hora local del PC. No guarda capturas ni cookies; los errores
pueden incluir textos de la interfaz: revisalos antes de compartirlos.
El panel distingue una senal reconocida sin entrada de una orden enviada
y conserva el motivo cuando un error detiene el detector.
Si hay figuras del color esperado dentro de la vela actual pero no pasan
el reconocimiento, el panel indica sus dimensiones frente a la muestra o
la coincidencia frente al umbral del 70 %. El diagnostico registra tambien
las transiciones de reconocimiento y si una flecha espera ausencia previa,
capturas consecutivas o ya fue registrada en el intervalo. Una figura de
color no se considera automaticamente una flecha ni habilita una entrada.

- No utiliza APIs de trading. Depende del DOM de la web PWA observada;
  cambios de idioma, estructura o campos pueden bloquearlo.
- La consulta requiere tarjetas de operaciones legibles en la cartera. Si
  no hay historial visible o aun esta cargando, mantiene la solicitud
  pendiente y bloquea nuevas entradas, sin inventar un resultado.
- La comprobacion visual de Cuenta demo no es una garantia atomica contra
  cambios de cuenta concurrentes. Nunca lo uses ni lo armes en cuenta real.
- El reconocimiento visual puede equivocarse y las flechas de una vela
  abierta pueden desaparecer. No predice resultados ni garantiza ganancias.
- El perfil `.iq_option_demo_profile` contiene sesion/cookies locales.
  No compartirlo, subirlo a repositorios ni exportar sus credenciales.
- Las pruebas de ejecucion usan mocks o una pagina ficticia local. No se
  enviaron operaciones reales ni demo durante el desarrollo. Hace falta
  validar la confirmacion y el cierre de una operacion en tu ventana demo
  bajo tu supervision antes de confiar en los totales.
