# Rendimiento del emulador (c64py)

Este documento resume **dónde se gasta el tiempo** hoy y **líneas de mejora** razonables, para orientar trabajo futuro sin comprometer exactitud opcional (VIC preciso, trazas, depuración Bruce Lee, etc.).

## Resumen

- El bucle principal es **`C64.run()` → `cpu.step()`** una vez por instrucción 6502.
- Con **`accurate_vic`**, cada **ciclo de CPU** avanza también el modelo VIC (`ViciiCycleEngine.tick()`), el SID y los CIAs: es el modo más caro pero necesario para alinear IRQ/badlines con VICE en casos difíciles.
- Sin VIC preciso, el raster avanza en **rachas** (`_advance_raster`), mucho más barato pero menos fiel.
- **`MemoryMap.read` / `write`** concentran la lógica de bancas 6510, I/O, hooks de depuración opcionales y (en rutas calientes) muchas ramas.

## Componentes relevantes

| Área | Archivos / notas |
|------|------------------|
| CPU | `cpu.py`: `step()` con gran cadena `if/elif` por opcode; cada instrucción llama `_mr`/`_mw` varias veces. |
| Memoria | `memory.py`: `read`/`write` con decodificación por rangos; flags/env vars activan logging costoso en rutas calientes. |
| VIC | `vicii_cycle.py` + integración en `cpu.py` (`_vic_tick_one` vs `_advance_raster`). |
| Vídeo host | `graphics.py`: pygame; coste moderado frente al núcleo salvo escalas altas. |
| Audio | `resid.py` / SID; puede competir con CPU según buffer y host. |
| Disco / IEC | `drive1541.py`, `iec_bus.py` cuando hay actividad de disco. |

## Cómo medir (ahora)

1. **cProfile** (tiempo por función):

   ```bash
   cd /ruta/al/repo/c64py
   python -m cProfile -o /tmp/c64py.prof C64.py --max-cycles 2000000 /ruta/a/test.prg
   python -c "import pstats; pstats.Stats('/tmp/c64py.prof').sort_stats('cumulative').print_stats(40)"
   ```

   Ajustar flags según `python C64.py --help` (por ejemplo `--accurate-vic`, sin `--graphics` para medir solo el núcleo).

2. **Estadísticas sobre `cpu.step`**  
   Contar invocaciones y ciclos emulados por segundo en un modo fijo (`accurate_vic` on/off) para comparar A/B.

3. **`py-spy` / `scalene`** (opcional)  
   Útiles en macOS/Linux para ver tiempo real sin tantos sesgos de `cProfile` en código C extension (ReSID).

## Benchmark reproducible (c64py)

Objetivo: **misma carga de trabajo** y **mismos parámetros** entre ejecuciones, y una línea **parseable** para scripts o cuadernos.

### Programa de referencia

- Fuente: `src/BENCHMARK.BAS` (relleno de pantalla, cambios de borde/fondo, matemática BASIC, `POKE` en pantalla y color).
- Binario: `programs/benchmark.prg` — generar con `./compile.sh` (requiere **petcat** de VICE).

### Invocación c64py

`--benchmark` implica `--turbo`, `--autoquit`, `--no-colors` y, si no pasas otro `.prg`, carga `programs/benchmark.prg`. También fuerza **`--headless`** salvo que uses `--graphics`.

Ejemplos (ajusta `--rom-dir` a donde tengas `kernal`, `basic`, etc.):

```bash
cd /ruta/al/repo/c64py

# Rápido (VIC “fast”), 20M ciclos
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms

# Mismo tope con VIC preciso (más lento, más fiel)
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms --accurate-vic
```

Al terminar verás el resumen humano (`=== Emulation Speed ===`) y **una línea JSON** prefijada con `C64PY_BENCHMARK ` (útil para `grep` o pipelines):

```bash
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms 2>/dev/null | grep '^C64PY_BENCHMARK '
```

Campos relevantes del JSON:

| Campo | Significado |
|--------|-------------|
| `cycles` | Ciclos de CPU emulados al parar (suele coincidir con `--max-cycles` si se alcanza el tope). |
| `wall_seconds` | Tiempo host (desde el arranque del bucle CPU en el emulador). |
| `emulated_cpu_mhz` | `cycles / wall_seconds` (rendimiento bruto en esta máquina). |
| `accurate_vic` | `true` / `false`. |
| `max_cycles_arg` | Lo pasado a `--max-cycles`. |
| `prg` | Nombre del programa (p. ej. `benchmark.prg`). |

**Nota:** Si `max_cycles` es bajo, el PRG puede no llegar a imprimir “benchmark complete”. Sube el tope (p. ej. `50000000` o más) hasta que en un volcado de pantalla veas el final del test, o quédate con comparaciones **solo por MHz** con el mismo `max_cycles` entre flags (`--accurate-vic` on/off).

### Script A/B rápido (fast vs accurate VIC)

```bash
chmod +x scripts/run_benchmark.sh
ROM_DIR=/ruta/a/roms BENCHMARK_CYCLES=20000000 ./scripts/run_benchmark.sh
# o
./scripts/run_benchmark.sh /ruta/a/roms
```

### Comparar con VICE (referencia cualitativa)

VICE no expone en CLI un “ejecuta exactamente N ciclos de CPU y sal” portable en todas las versiones, así que la comparación suele ser:

1. **Misma carga**: autostart de `programs/benchmark.prg` en VICE con **warp** activado y medir **tiempo de pared** hasta que en pantalla aparezca `benchmark complete` (cronómetro o grabación).
2. **C64py**: usar un `--max-cycles` alto enough para que el mensaje aparezca (o comparar solo MHz con ciclos fijos, que es 100 % reproducible en c64py).

Ejemplo orientativo (ajusta rutas y binario `x64sc` / `x64`):

```bash
x64sc -warp -sounddev dummy -autostartprgpath programs/benchmark.prg +confirmexit
```

Los **jiffies** que imprime el BASIC (`TI` / `TI$`) dependen del **reloj emulado** (PAL/NTSC), no del host: sirven para comparar **exactitud** entre VICE y c64py si el programa llega a completarse en ambos, mientras que `emulated_cpu_mhz` mide **cuánto host** necesitas para avanzar N ciclos.

## Cuellos de botella probables (hipótesis ordenadas)

1. **Interpretación Python por opcode**  
   Una tabla de punteros a funciones o un `match`/dispatch más compacto puede reducir overhead del intérprete; hay que mantener o duplicar la lógica de ciclos/trazas.

2. **`MemoryMap.read`**  
   Muchas comprobaciones secuenciales; para rutas “solo RAM” se podría introducir un **fast path** cuando `$01` y el mapa lo permitan (con cuidado con CHAREN/I/O).

3. **VIC preciso = 1× `tick()` por ciclo de CPU**  
   Micro-optimizar `ViciiCycleEngine.tick()` (atributos locales, menos tuplas) o mover el núcleo a **extensión C/Rust** si hiciera falta otro orden de magnitud.

4. **Depuración condicional**  
   Bruce Lee / loader: asegurar que los `if self._brucelee_debug_enabled` fallen en la primera condición y no evalúen rangos de dirección cuando está off (revisar hot path).

5. **Traces y UDP**  
   Con `trace_enabled` o UDP activo, el coste domina; benchmarks de “velocidad máxima” deben desactivarlos explícitamente.

## Direcciones de mejora (roadmap suave)

- **Corto plazo**: perfilar con un juego/demo fijo y `--accurate-vic` on/off; documentar MHz emulado logrado en README o aquí.
- **Medio plazo**: tabla de dispatch para opcodes “comunes” (LDA/STA/branch) sin tocar los raros.
- **Largo plazo**: extensión nativa para CPU+mem+VIC en el hot loop, o PyPy (validar compatibilidad con pygame/resid).

## Referencias internas

- Aceleración / throttling: `emulator.py` — `throttle_emulation_if_needed`, `turbo`.
- Modo VIC: flag `accurate_vic` al construir `CPU` (ver `C64.py` / arranque del emulador).
