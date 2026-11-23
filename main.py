"""Qullamaggie Fused Bot — escanea TODOS los perpetuos USDT de BingX y envía señales detalladas a Telegram.

Flujo (cada cierre de vela del TIMEFRAME):
  1. Refresca el universo (cada 6 h) y los datos diarios (una vez por día UTC).
  2. Pre-filtra candidatos con UN solo fetch_tickers + contexto diario (superconjunto conservador).
  3. Descarga velas intradía solo de los candidatos y re-simula la estrategia (strategy.replay).
  4. Envía a Telegram los eventos nuevos: ENTRY, TP1, STOP, BE_STOP, TRAIL, TIME.
"""
import asyncio
import json
import logging
import os
import signal
import time

import numpy as np

import messages as M
import strategy as S
from bingx import BingX
from config import cfg
from telegram_bot import Telegram

logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

DAY_MS = S.DAY_MS


def tf_seconds(tf):
    unit, val = tf[-1], int(tf[:-1])
    return val * {"m": 60, "h": 3600, "d": 86400}[unit]


class Bot:
    def __init__(self):
        self.bx = BingX(cfg)
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, cfg.telegram_thread_id)
        self.tf_s = tf_seconds(cfg.timeframe)
        self.tf_ms = self.tf_s * 1000
        self.universe_at = 0.0
        self.daily = {}          # symbol -> series diarias
        self.daily_day = None    # día UTC de la última carga diaria
        self.prior_pot = set()   # símbolos con potencial en días previos de la ventana
        self.seen = {}           # clave de evento -> timestamp (dedupe)
        self.open_trades = {}
        self.counters = {"day": None, "entries": 0, "exits": 0}
        self.last_stats = {"universe": 0, "daily_ok": 0, "cands": 0, "secs": 0.0}
        self.last_heartbeat = time.time()
        self.last_error_alert = 0.0
        self.stop = asyncio.Event()

    # ───────────── estado (dedupe) ─────────────
    def load_state(self):
        try:
            with open(cfg.state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.seen = {k: int(v) for k, v in d.get("seen", {}).items()}
            log.info("Estado cargado: %d eventos vistos", len(self.seen))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("No se pudo leer el estado: %s", e)

    def save_state(self):
        try:
            cutoff = int(time.time()) - 5 * 86400
            self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
            folder = os.path.dirname(cfg.state_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = cfg.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seen": self.seen}, f)
            os.replace(tmp, cfg.state_path)
        except Exception as e:
            log.debug("No se pudo guardar el estado: %s", e)

    # ───────────── datos ─────────────
    async def refresh_daily(self, today, now_ms):
        log.info("Descargando velas diarias de %d pares...", len(self.bx.symbols))
        t0 = time.time()

        async def one(sym):
            try:
                raw = await self.bx.fetch_ohlcv(sym, "1d", 150)
            except Exception as e:
                log.debug("Diario %s falló: %s", sym, e)
                return sym, None
            if raw is None:
                return sym, None
            raw = raw[raw[:, 0] + DAY_MS <= now_ms]  # solo velas diarias cerradas
            if len(raw) < 35:
                return sym, None
            return sym, raw

        res = await asyncio.gather(*(one(s) for s in self.bx.symbols))
        daily, pot, misaligned = {}, set(), 0
        for sym, raw in res:
            if raw is None:
                continue
            if raw[-1, 0] % DAY_MS != 0:
                misaligned += 1
            ds = S.daily_series(raw[:, 0], raw[:, 1], raw[:, 2], raw[:, 3], raw[:, 4], raw[:, 5], cfg.ps_lookback)
            daily[sym] = ds
            if S.prior_days_potential(ds, cfg, today):
                pot.add(sym)
        if daily and misaligned > len(daily) * 0.5:
            log.warning("Las velas diarias de BingX no parecen alineadas a 00:00 UTC (%d/%d). "
                        "Revisa el cálculo de contexto diario.", misaligned, len(daily))
        self.daily, self.prior_pot, self.daily_day = daily, pot, today
        log.info("Diario listo: %d pares con histórico (%d con potencial previo) en %.0fs",
                 len(daily), len(pot), time.time() - t0)

    def select_candidates(self, tickers, today):
        cands = []
        for sym in self.bx.symbols:
            ds = self.daily.get(sym)
            t = tickers.get(sym)
            if ds is None or not t:
                continue
            if cfg.min_quote_volume > 0 and (t.get("quoteVolume") or 0) < cfg.min_quote_volume:
                continue
            hi = t.get("high")
            if sym in self.prior_pot or (hi and S.could_signal(S.ctx_for_day(ds, today), float(hi), cfg)):
                cands.append(sym)
        return cands

    # ───────────── ciclo ─────────────
    async def cycle(self):
        t0 = time.time()
        now_ms = int(t0 * 1000)
        today = now_ms // DAY_MS
        if self.counters["day"] != today:
            self.counters.update(day=today, entries=0, exits=0)
        if not self.bx.symbols or t0 - self.universe_at > 6 * 3600:
            await self.bx.load_universe()
            self.universe_at = t0
        if self.daily_day != today:
            await self.refresh_daily(today, now_ms)

        tickers = await self.bx.fetch_tickers()
        cands = self.select_candidates(tickers, today)
        n_bars = min(1440, int(cfg.replay_days * 1440 / (self.tf_s / 60)) + 220)

        async def work(sym):
            try:
                raw = await self.bx.fetch_ohlcv(sym, cfg.timeframe, n_bars)
                if raw is None:
                    return sym, None, "sin velas"
                raw = raw[raw[:, 0] + self.tf_ms <= now_ms]  # descarta la vela en formación
                if len(raw) < 100:
                    return sym, None, "pocas velas"
                bars = (raw[:, 0].astype(np.int64), raw[:, 1], raw[:, 2], raw[:, 3], raw[:, 4], raw[:, 5])
                return sym, S.replay(sym, bars, self.daily[sym], cfg, self.tf_ms), None
            except Exception as e:
                return sym, None, f"{type(e).__name__}: {e}"

        results = await asyncio.gather(*(work(s) for s in cands))

        errors = [(s, e) for s, r, e in results if e]
        opens, to_send = {}, []
        max_age = cfg.max_event_age_bars * self.tf_ms
        now_s = int(time.time())
        for sym, res, err in results:
            if res is None:
                continue
            if res["open_trade"]:
                opens[sym] = res["open_trade"]
            for ev in res["events"]:
                key = f"{sym}|{ev['signal_ts']}|{ev['type']}"
                if key in self.seen:
                    continue
                self.seen[key] = now_s
                if now_ms - ev["event_ts"] > max_age:
                    continue  # evento antiguo: se marca como visto sin enviarlo
                if ev["type"] != "ENTRY" and not cfg.send_exit_updates:
                    continue
                to_send.append(ev)
        self.open_trades = opens

        to_send.sort(key=lambda e: e["event_ts"])
        for ev in to_send:
            await self.tg.send(M.format_event(ev, cfg, now_ms))
            self.counters["entries" if ev["type"] == "ENTRY" else "exits"] += 1
            log.info("SEÑAL %s %s %s", ev["type"], ev["setup"], ev["symbol"])
        self.save_state()

        secs = time.time() - t0
        self.last_stats = {"universe": len(self.bx.symbols), "daily_ok": len(self.daily),
                           "cands": len(cands), "secs": secs, **{k: self.counters[k] for k in ("entries", "exits")}}
        log.info("Ciclo OK · universo=%d candidatos=%d abiertas=%d enviados=%d errores=%d · %.1fs",
                 len(self.bx.symbols), len(cands), len(opens), len(to_send), len(errors), secs)
        if len(cands) >= 8 and len(errors) > len(cands) * 0.3:
            await self.alert(f"{len(errors)}/{len(cands)} pares fallaron. Ej.: {errors[0][0]} -> {errors[0][1]}")

        if time.time() - self.last_heartbeat >= cfg.heartbeat_hours * 3600 > 0:
            self.last_heartbeat = time.time()
            await self.tg.send(M.heartbeat(self.last_stats, self.open_trades, cfg))

    async def alert(self, text):
        if time.time() - self.last_error_alert < 1800:
            return
        self.last_error_alert = time.time()
        await self.tg.send(M.error_alert(text))

    # ───────────── ejecución ─────────────
    async def run(self):
        self.load_state()
        await self.tg.start()
        while not self.stop.is_set():  # el universo debe cargar antes de arrancar
            try:
                await self.bx.load_universe()
                self.universe_at = time.time()
                break
            except Exception as e:
                log.error("No se pudo cargar el universo de BingX: %s", e)
                await self.alert(f"No se puede conectar con BingX: {e}. Si persiste, cambia la región del servicio en Railway.")
                await asyncio.sleep(60)
        if not self.stop.is_set():
            await self.tg.send(M.startup(cfg, len(self.bx.symbols)))

        while not self.stop.is_set():
            try:
                await self.cycle()
            except Exception as e:
                log.exception("Fallo en el ciclo")
                await self.alert(f"{type(e).__name__}: {e}")
            now = time.time()
            nxt = (int(now) // self.tf_s + 1) * self.tf_s + cfg.bar_close_delay
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(1.0, nxt - now))
            except asyncio.TimeoutError:
                pass

        await self.tg.close()
        await self.bx.close()
        log.info("Bot detenido.")


async def main():
    bot = Bot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, bot.stop.set)
        except NotImplementedError:
            pass
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
