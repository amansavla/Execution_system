import logging
from datetime import datetime
from src.app.runner import StrategyProvider
from src.core.config import StrategyConfig
from src.core.models import StrategySignal
from src.portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)


class CompositeStrategyProvider(StrategyProvider):
    """Dispatches polling calls to the appropriate strategy provider based on config."""

    def __init__(self, providers: dict[str, StrategyProvider]) -> None:
        """Initialize with a mapping of signal_source to StrategyProvider."""
        self.providers = providers

    def set_position_manager(self, position_manager: PositionManager) -> None:
        """Propagate position manager to all sub-providers."""
        for provider in self.providers.values():
            if hasattr(provider, "set_position_manager"):
                provider.set_position_manager(position_manager)

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        """Route poll to the provider registered for strategy_config.entry.signal_source."""
        source = strategy_config.entry.signal_source
        provider = self.providers.get(source)
        if not provider:
            logger.warning(
                "No strategy provider registered for signal source '%s' (strategy: %s)",
                source,
                strategy_config.strategy_id
            )
            return []
        return await provider.poll(strategy_config, current_time)

    async def collect_exits(self, strategy_config: StrategyConfig, current_time: datetime):
        """Route strategy-driven exit collection to the matching provider."""
        provider = self.providers.get(strategy_config.entry.signal_source)
        if not provider or not hasattr(provider, "collect_exits"):
            return set()
        return await provider.collect_exits(strategy_config, current_time)
