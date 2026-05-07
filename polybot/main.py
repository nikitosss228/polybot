"""Entrypoint: scan → score → (maybe trade) loop, async + graceful shutdown."""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .client import PolyClient
from .config import SETTINGS
from .equity import EquityTracker
from .logger import get_logger, setup_logging
from .mm_exit import ExitManager
from .models import Candidate
from .orders import FillEvent, OrderState, OrderTracker, TrackedOrder
from .positions import Position, PositionRegistry
from .risk import RiskManager
from .scanner import scan
from .sim_wallet import SimulatedWallet, simulate_entry
from .study import StudyLogger

log = get_logger(__name__)


def _format_candidate_line(idx: int, c: Candidate) -> str:
    days = c.market.days_to_end
    days_str = f"{days:+.1f}d" if days is not None else "n/a "
    return (
        f"  #{idx:>2} | edge={c.edge_pct:>5.2f}% | conf={c.confidence:.2f} | "
        f"score={c.score:>5.2f} | end={days_str} | vol24h=${c.market.volume_24h:>10,.0f} | "
        f"{c.outcome.name:<6} @ {c.entry_price:.3f} | "
        f"{c.market.question[:60]}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _process_fill_events(
    events: List[FillEvent],
    registry: PositionRegistry,
    exitmgr: ExitManager,
    rm: RiskManager,
) -> None:
    """Translate OrderTracker FillEvents into PositionRegistry / ExitManager state.

    BUY fills → open a Position from intent_payload; if mm_spread,
    schedule the matching sell. SELL fills → close the parent Position
    at the actual fill price, update ExitManager, push realized PnL
    into RiskManager.
    """
    for ev in events:
        order = ev.order
        if ev.new_state == OrderState.PARTIALLY_FILLED:
            log.info("Order partially filled: %s %s shares=%.2f/%.2f",
                     order.order_id[:12], order.side,
                     order.size_matched, order.size)
            continue
        if ev.new_state == OrderState.CANCELLED:
            log.info("Order cancelled: %s %s (final=%s)",
                     order.order_id[:12], order.side, order.final_status)
            continue
        if ev.new_state != OrderState.FILLED:
            continue

        if order.side == "BUY":
            ip = order.intent_payload or {}
            try:
                pos = registry.open_from_fill(
                    token_id=order.token_id,
                    condition_id=ip.get("condition_id", ""),
                    slug=ip.get("slug", ""),
                    outcome_name=ip.get("outcome_name", ""),
                    outcome_index=int(ip.get("outcome_index", 0)),
                    entry_price=order.price,
                    entry_size_usd=order.size_usd,
                    detector=ip.get("detector", ""),
                )
            except ValueError as exc:
                log.warning("BUY fill but position open rejected: %s", exc)
                continue
            # If this was an mm_spread buy, queue the matching sell.
            if pos.detector == "mm_spread":
                exitmgr.schedule_sell(
                    token_id=pos.token_id,
                    condition_id=pos.condition_id,
                    slug=pos.slug,
                    shares=pos.shares,
                    entry_price=pos.entry_price,
                    spread_at_buy=float(ip.get("spread_at_buy", 0.0)),
                )
        elif order.side == "SELL":
            sell_price = order.price
            exitmgr.mark_done(order.token_id, sell_price)
            closed = registry.close_position(order.token_id, sell_price)
            if closed is not None and closed.realized_pnl is not None:
                rm.record_close(order.token_id, closed.realized_pnl)
            else:
                log.warning("SELL fill on %s but no parent position to close",
                            order.token_id[:12])


async def _record_equity(
    equity: Optional[EquityTracker],
    pc: PolyClient,
    rm: RiskManager,
    registry: Optional[PositionRegistry],
    sim_wallet: Optional[SimulatedWallet] = None,
) -> None:
    if equity is None:
        return
    try:
        if SETTINGS.dry_run:
            equity.record_paper_sample(registry, rm, sim_wallet=sim_wallet)
        else:
            await equity.record_live_sample(pc.get_usdc_balance, registry, rm)
    except Exception as exc:  # noqa: BLE001
        log.warning("equity sample failed: %s", exc)


async def run_tick(
    pc: PolyClient,
    rm: RiskManager,
    study: Optional[StudyLogger] = None,
    registry: Optional[PositionRegistry] = None,
    tracker: Optional[OrderTracker] = None,
    exitmgr: Optional[ExitManager] = None,
    equity: Optional[EquityTracker] = None,
    sim_wallet: Optional[SimulatedWallet] = None,
) -> None:
    log.info("Tick start — fetching markets")
    markets = await pc.fetch_active_markets(
        min_volume_24h=SETTINGS.min_24h_volume,
        max_markets=2000,
    )
    log.info("Got %d tradable markets above $%s vol24h", len(markets), SETTINGS.min_24h_volume)

    # 1. Reconcile pending orders with the CLOB and react to fills.
    # Skipped in dry-run (tracker is empty there).
    if tracker is not None and registry is not None and exitmgr is not None and tracker.active:
        try:
            events = await tracker.reconcile(pc.get_order_status)
            if events:
                log.info("Tick: %d order state transitions", len(events))
                await _process_fill_events(events, registry, exitmgr, rm)
        except Exception as exc:  # noqa: BLE001
            log.exception("Order reconcile failed: %s", exc)

    # 2. Post any sells that were scheduled (mm_spread buys that filled).
    if exitmgr is not None and tracker is not None and exitmgr.pending_post:
        try:
            await exitmgr.tick(pc.place_sell_limit, pc.get_book_summary, tracker)
        except Exception as exc:  # noqa: BLE001
            log.exception("ExitManager.tick failed: %s", exc)

    # 3. Resolution sweep — close any of our open positions whose markets
    # have settled since the last tick, so realized PnL hits the daily /
    # cumulative counters before we gate this tick's new candidates.
    if registry is not None and registry.open_positions:
        def _on_close(pos: Position) -> None:
            if pos.realized_pnl is not None:
                rm.record_close(pos.token_id, pos.realized_pnl)
            # In simulation mode, also pay out the simulated wallet:
            # payoff = shares × terminal_price (UMA settlement, no fee).
            if sim_wallet is not None and pos.terminal_price is not None:
                payoff = pos.shares * pos.terminal_price
                sim_wallet.record_close(payoff, pos.slug)
        try:
            # closed_only=False so registry can soft-resolve markets
            # Polymarket left at closed=False but with outcomePrices at
            # extreme + endDate already past (UMA settlement lag).
            async def _fetch(cids):
                return await pc.fetch_markets_by_condition_ids(cids, closed_only=False)
            newly_closed = await registry.check_resolutions(_fetch, _on_close)
            if newly_closed:
                log.info("Tick: closed %d resolved positions", len(newly_closed))
        except Exception as exc:  # noqa: BLE001
            log.exception("Resolution sweep failed: %s", exc)

    candidates: List[Candidate] = scan(markets)
    log.info("Detector output: %d candidates", len(candidates))

    if study is not None:
        ts = study.begin_tick()
        sizes = {c.outcome.token_id: rm.position_size(c) for c in candidates}
        n_cand = study.write_candidates(ts, candidates, sizes)
        n_track = study.write_price_track(ts, markets)
        log.info(
            "Study tick #%d: wrote %d candidate rows, %d price-track rows (tracking %d tokens)",
            study.tick_id, n_cand, n_track, len(study.tracked),
        )

    if not candidates:
        log.info("No candidates this tick.")
        await _record_equity(equity, pc, rm, registry, sim_wallet)
        return

    log.info("Top candidates:")
    for i, c in enumerate(candidates[:15]):
        log.info(_format_candidate_line(i + 1, c))

    if not SETTINGS.auto_trade:
        log.info("AUTO_TRADE=0 — surfacing candidates only, not placing orders.")
        await _record_equity(equity, pc, rm, registry, sim_wallet)
        return

    # Auto-trade path: gate each candidate by risk caps and place orders.
    placed = 0
    for c in candidates:
        reason = rm.gate(c)
        if reason is not None:
            log.info("Skip %s: %s", c.market.slug, reason)
            continue
        # Also skip if we already have a pending buy on this token from a
        # previous tick — gate.has_open_token only catches FILLED positions.
        if tracker is not None and tracker.has_pending_buy(c.outcome.token_id):
            log.info("Skip %s: pending buy already on book", c.market.slug)
            continue
        size = rm.position_size(c)
        if size <= 0:
            log.info("Skip %s: zero size after caps", c.market.slug)
            continue
        log.info(
            "Placing BUY %s @ %.3f size=$%.2f on %s",
            c.outcome.name, c.entry_price, size, c.market.slug,
        )
        try:
            resp = await pc.place_buy_limit(c.outcome.token_id, c.entry_price, size)
        except Exception as exc:  # noqa: BLE001
            log.exception("Order placement failed for %s: %s", c.market.slug, exc)
            continue
        log.info("Order resp: %s", resp)
        is_dry_run_resp = isinstance(resp, dict) and resp.get("dry_run")

        # Simulation path: dry_run dict comes back from place_buy_limit but
        # we treat it as filled immediately, with simulated entry-price
        # slippage and an estimated taker fee deducted from the paper wallet.
        # The position registers in the real Position registry so the
        # resolution sweep closes it correctly via Gamma.
        if is_dry_run_resp and SETTINGS.simulation_mode and sim_wallet is not None \
                and registry is not None:
            sim = simulate_entry(
                candidate_entry_price=c.entry_price,
                token_id=c.outcome.token_id,
                tick_id=int(study.tick_id) if study else 0,
                slug=c.market.slug,
                size_usd=size,
            )
            try:
                sim_wallet.record_open(size, sim["fee"], c.market.slug)
            except ValueError as exc:
                log.info("Skip %s: %s", c.market.slug, exc)
                continue
            outcome_index = next(
                (i for i, o in enumerate(c.market.outcomes)
                 if o.token_id == c.outcome.token_id), 0)
            try:
                registry.open_from_fill(
                    token_id=c.outcome.token_id,
                    condition_id=c.market.condition_id,
                    slug=c.market.slug,
                    outcome_name=c.outcome.name,
                    outcome_index=outcome_index,
                    entry_price=sim["filled_price"],
                    entry_size_usd=size,
                    detector=c.detector,
                )
            except ValueError as exc:
                log.warning("sim position open rejected: %s", exc)
                # Refund wallet so we don't accumulate ghost cost.
                sim_wallet.state.balance += size + sim["fee"]
                sim_wallet.state.cumulative_fees_paid -= sim["fee"]
                sim_wallet._save()
                continue
            log.info("SIM fill: %s %s entry=%.4f (slip %+.4f) fee=$%.4f shares=%.2f",
                     c.market.slug, c.outcome.name, sim["filled_price"],
                     sim["slippage"], sim["fee"], sim["shares"])
            placed += 1
            continue

        if resp and not is_dry_run_resp:
            # Real submission — order is now LIVE on the CLOB. Track it
            # and let reconcile() detect the fill on a future tick. Don't
            # touch the registry here: that happens via FillEvent.
            order_id = (resp.get("orderID") if isinstance(resp, dict) else None) \
                or (resp.get("order_id") if isinstance(resp, dict) else None) \
                or (resp.get("id") if isinstance(resp, dict) else None)
            if not order_id:
                log.error("place_buy_limit returned no order_id, can't track: %s", resp)
                continue
            if tracker is not None:
                try:
                    outcome_index = next(
                        (i for i, o in enumerate(c.market.outcomes)
                         if o.token_id == c.outcome.token_id), 0)
                    tracker.register_pending(TrackedOrder(
                        order_id=str(order_id),
                        side="BUY",
                        token_id=c.outcome.token_id,
                        price=c.entry_price,
                        size=round(size / c.entry_price, 4) if c.entry_price > 0 else 0.0,
                        size_usd=size,
                        posted_ts=_now_iso(),
                        intent_payload={
                            "slug": c.market.slug,
                            "condition_id": c.market.condition_id,
                            "outcome_name": c.outcome.name,
                            "outcome_index": outcome_index,
                            "detector": c.detector,
                            "spread_at_buy": c.market.spread,
                            "edge_pct": c.edge_pct,
                            "confidence": c.confidence,
                        },
                    ))
                except ValueError as exc:
                    log.warning("Order tracker rejected %s: %s", order_id, exc)
            rm.record_open(c, size)
        placed += 1
    log.info("Tick done — placed %d orders", placed)
    await _record_equity(equity, pc, rm, registry, sim_wallet)


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handle(sig: signal.Signals) -> None:
        log.info("Received %s, shutting down...", sig.name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: stop.set())


async def run() -> None:
    setup_logging()
    log.info(
        "Polybot starting — dry_run=%s auto_trade=%s budget=$%.0f scan_every=%ss",
        SETTINGS.dry_run, SETTINGS.auto_trade, SETTINGS.total_budget, SETTINGS.scan_interval_sec,
    )
    if SETTINGS.dry_run:
        log.info("DRY_RUN=1 — orders will only be logged, not submitted.")
    if not SETTINGS.has_wallet:
        log.info("No PRIVATE_KEY — scanner-only mode.")

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    # The order/position layer. All four objects are constructed in both
    # modes so the same code path runs in dry-run and live; in dry-run
    # they stay empty because place_buy_limit returns dry_run dicts that
    # don't get tracked.
    registry = PositionRegistry()
    tracker = OrderTracker()
    exitmgr = ExitManager()
    equity = EquityTracker()
    sim_wallet: Optional[SimulatedWallet] = None
    if SETTINGS.simulation_mode and SETTINGS.dry_run:
        sim_wallet = SimulatedWallet(starting_balance=SETTINGS.simulation_starting_usdc)
        log.info("SIMULATION_MODE: paper-trading wallet at $%.2f starting balance",
                 sim_wallet.balance)
    rm = RiskManager(registry=registry)
    study: Optional[StudyLogger] = StudyLogger() if SETTINGS.dry_run else None
    if study is not None:
        log.info(
            "Study mode: writing candidates.csv + price_track.csv to logs/ "
            "(resuming at tick #%d, %d tokens already tracked)",
            study.tick_id, len(study.tracked),
        )

    async with PolyClient() as pc:
        if SETTINGS.has_wallet:
            try:
                bal = await pc.get_usdc_balance()
                log.info("USDC balance: $%.2f", bal)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not fetch USDC balance: %s", exc)

        await run_tick(pc, rm, study, registry, tracker, exitmgr, equity, sim_wallet)
        while not stop.is_set():
            wait = SETTINGS.scan_interval_sec
            log.info("Sleeping %ds until next scan", wait)
            stopped = await _wait_or_stop(stop, wait)
            if stopped:
                break
            try:
                await run_tick(pc, rm, study, registry, tracker, exitmgr, equity, sim_wallet)
            except Exception as exc:  # noqa: BLE001
                log.exception("Tick failed: %s. Will retry next interval.", exc)

    log.info("Shutdown complete")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
