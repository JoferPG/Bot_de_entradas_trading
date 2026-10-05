# Detector CALL/PUT y ejecucion demo

Windows, Python 3.14, Microsoft Edge y las dependencias de
[requirements.txt](requirements.txt).

```powershell
Set-Location 'D:\07 - Script'
python -m pip install -r requirements.txt
python bot.py
```

Si Python no esta en PATH, usa la ruta completa de tu interprete.

## Uso

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
   de flechas y selecciona un solo grafico en tiempo real.
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
seguimiento normal (intervalo de 200 ms mas el tiempo de procesamiento),
sin parpadeo. El marco blanco sigue la vela actual identificada.
Las lineas se dibujan despues de reducir la vista para conservar su grosor.
Blanco indica la franja identificada de la vela actual. No se exige
movimiento del cuerpo/mecha. Si no se identifica una secuencia fiable,
se muestra el grafico sin lineas y se bloquean las entradas.
Si el seguimiento indica que la nueva vela aun no es identificable, la
vista en vivo continua sin marco; no se emiten senales ni entradas hasta
recuperar un seguimiento fiable.
El seguimiento admite cuerpos de un pixel de altura y agrupa fragmentos
de cuerpo/mecha dentro de una misma posicion del espaciado de velas.
Conserva los rechazos de objetos anchos, series ambiguas y posiciones
irregulares. Sigue siendo reconocimiento por imagen: una vela sin pixeles
del color configurado no se puede confirmar, y el grafico debe estar en vivo.
Una flecha CALL/PUT nueva dentro de la franja actual puede habilitar una
entrada aunque el cuerpo de la vela este quieto. Se mantienen dos capturas
sin flecha para rearmar, tres capturas consecutivas de reconocimiento y
como maximo una senal por intervalo. Las flechas presentes al iniciar y
las de velas anteriores no provocan entradas. Se conservan las comprobaciones
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

El panel separa CALL, PUT y TOTAL:

- Operaciones confirmadas por ID, pendientes y cerradas.
- Importe invertido en posiciones confirmadas.
- Importe devuelto en posiciones cerradas, leido del campo **Rendimiento**
  de los detalles de la interfaz observada.
- Neto cerrado = devuelto - inversion de esas posiciones cerradas.
  No se resta la inversion pendiente como si ya fuese una perdida.

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

El reconocimiento admite variaciones de borde de hasta un pixel de ancho
y dos de alto. Tambien acepta compresion horizontal desde 10 pixeles de
ancho hasta el ancho calibrado: ajusta solo el ancho de la silueta de
referencia y exige coincidencia >=70 %. Con una muestra de 23x27 puede
validar 10x27 o 16x27, pero no acepta una figura por dimensiones solamente.
Mantiene color, altura y comparacion de forma; las figuras por debajo del
70 % siguen rechazadas, pero algunas flechas recortadas pueden superar
este umbral. No amplia la tolerancia al zoom vertical. Una figura rechazada del color esperado
no cuenta como captura sin flecha para rearmar: evita habilitar como nueva
una flecha que solo alterna entre reconocida y rechazada. Se mantienen tres
capturas consecutivas y el bloqueo de flechas ya visibles al cambiar de vela.
El umbral del 70 % permite mayor variacion de forma, pero aumenta el riesgo
de aceptar figuras que no sean flechas respecto al umbral anterior del 88 %.
Las tres capturas usan ahora pausas de 200 ms: al menos 400 ms entre la
primera y la tercera, mas el procesamiento. Las mascaras de color se
calculan con Pillow; la mascara de velas identifica la franja actual. Si Tiempo ya coincide con el cierre, no se repite la lectura
completa de configuracion durante su preparacion; se conserva la lectura
inicial y la comprobacion completa inmediatamente antes del clic.
Esto reduce trabajo, pero no extiende el horario de compra que permite
IQ Option ni sustituye un vencimiento no disponible por otro.
Tras reiniciar pulsa INICIAR: si existe una solicitud pendiente, las
entradas quedan bloqueadas y su validacion se programa automaticamente.
Una vez resuelta, vuelve a pulsar INICIAR para activar nuevas entradas.

## Pruebas desde VS Code

La configuracion de `.vscode/settings.json` habilita unittest y descubre
`test_*.py` en el panel Testing. Selecciona el interprete con Pillow y
Playwright instalados, actualiza el descubrimiento y ejecuta las pruebas.
No requieren abrir IQ Option ni enviar ordenes. Tambien pueden ejecutarse
desde la terminal integrada:

```powershell
python -m unittest -q test_bot test_demo_execution
```

Las regresiones cubren cuerpos diminutos, franjas de seguimiento,
reconocimiento de flechas sobre velas quietas y protecciones DEMO.

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
