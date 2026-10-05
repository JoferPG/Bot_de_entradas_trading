-- Quadcode Script (QCS), basado en Lua; no es Pine Script.
-- Indicador visual: no envia ordenes ni ejecuta operaciones.
instrument {
    name = "EMA + RSI - CALL / PUT",
    short_name = "EMA RSI",
    overlay = true
}

-- Periodos configurables. El minimo permitido es un periodo.
fastPeriod = input(9, "Periodo EMA rapida", input.integer, 1)
slowPeriod = input(21, "Periodo EMA lenta", input.integer, 1)
rsiPeriod = input(14, "Periodo RSI", input.integer, 1)

-- Ambas medias utilizan el precio de cierre.
fastEMA = ema(close, fastPeriod)
slowEMA = ema(close, slowPeriod)

-- RSI de Wilder: suavizar por separado ganancias y perdidas.
-- ssma comienza con una SMA y luego aplica el suavizado de Wilder.
delta = close - close[1]
averageGain = ssma(max(delta, 0), rsiPeriod)
averageLoss = ssma(max(-delta, 0), rsiPeriod)

-- Formula equivalente al RSI habitual, sin division entre cero.
-- Sin perdidas: 100. Sin ganancias: 0. Ambas en cero: 50.
rsiSeries = make_series()
local gain = get_value(averageGain)
local loss = get_value(averageLoss)

if not isnan(gain) and not isnan(loss) then
    local total = gain + loss
    if total > 0 then
        rsiSeries:set(100 * gain / total)
    else
        rsiSeries:set(50)
    end
end

-- Valores actuales y de la vela anterior para detectar el cruce.   
local fastNow = get_value(fastEMA)
local slowNow = get_value(slowEMA)
local fastPrevious = get_value(fastEMA[1])
local slowPrevious = get_value(slowEMA[1])
local rsiNow = get_value(rsiSeries)

-- Esperar hasta disponer de datos validos para los indicadores.
local dataReady = not isnan(fastNow)
    and not isnan(slowNow)
    and not isnan(fastPrevious)
    and not isnan(slowPrevious)
    and not isnan(rsiNow)

-- CALL: cruce alcista en esta vela y RSI mayor que 50.
callSignal = conditional(
    dataReady
    and fastNow > slowNow
    and fastPrevious <= slowPrevious
    and rsiNow > 50
)

-- PUT: cruce bajista en esta vela y RSI menor que 50.
putSignal = conditional(
    dataReady
    and fastNow < slowNow
    and fastPrevious >= slowPrevious
    and rsiNow < 50
)

-- Mostrar las EMAs; el RSI se utiliza solamente como filtro.
plot(fastEMA, "EMA rapida", "#00BFFF", 2)
plot(slowEMA, "EMA lenta", "#FFA500", 2)

-- CALL: flecha verde debajo de la vela, desplazamiento cero.
plot_shape(
    callSignal, "CALL", shape_style.arrowup, shape_size.large,
    "#00C853", shape_location.belowbar, 0, "CALL", "#00C853"
)

-- PUT: flecha roja encima de la vela, desplazamiento cero.
plot_shape(
    putSignal, "PUT", shape_style.arrowdown, shape_size.large,
    "#FF1744", shape_location.abovebar, 0, "PUT", "#FF1744"
)

-- La senal de la vela abierta puede cambiar hasta que cierre.