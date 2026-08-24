"""Orchestrates one discovery cycle: tag markets -> grade trades -> recompute
stats -> promote candidates -> forward-track. Runs in this fixed order every
cycle, each stage bounded (see each module's own per-cycle cap), so the
whole sweep is a single scheduled, checkpointed, resumable job - killing it
mid-cycle loses nothing, the next cycle resumes from each stage's own
checkpoint.
"""

import asyncio
import logging

from app.collectors.polymarket import PolymarketClient
from app.config.settings import Settings
from app.discovery.forward import run_discovery_forward_tracking
from app.discovery.promotion import run_niche_promotion
from app.discovery.stats import run_niche_stats
from app.discovery.trades import run_trade_grading_walk
from app.discovery.walk import run_niche_tagging_walk

logger = logging.getLogger(__name__)


async def run_discovery_sweep(client: PolymarketClient, settings: Settings) -> None:
    tagging = await run_niche_tagging_walk(client, settings)
    trading = await run_trade_grading_walk(client, settings)
    await asyncio.to_thread(run_niche_stats, settings)
    await asyncio.to_thread(run_niche_promotion, settings)
    await asyncio.to_thread(run_discovery_forward_tracking, settings)
    logger.info(
        "discovery: sweep complete - tagged=%d/%d graded_trades=%d",
        tagging.classified,
        tagging.fetched,
        trading.trades_graded,
    )
