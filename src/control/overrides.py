"""OverrideManager — read/write runtime overrides.

Manages the mutable override state that ManualControlService
modifies. Overrides are kept in-memory for fast access and
persisted to configs/overrides.yaml on mutation.

Override state feeds into RiskEngine's OverridesConfig.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

from src.core.config import OverridesConfig

logger = logging.getLogger(__name__)


class OverrideManager:
    """In-memory override state with optional YAML persistence.

    Provides typed read/write access to:
    - paused_strategies
    - disabled_symbols
    - reduce_only (global)
    - reduce_only_strategies (per-strategy)
    - system_locked

    Mutations are reflected immediately in .state and optionally
    persisted to disk via save().
    """

    def __init__(
        self,
        initial_state: Optional[OverridesConfig] = None,
        persist_path: Optional[Path] = None,
    ) -> None:
        """Initialize OverrideManager.

        Args:
            initial_state: Starting override state. Defaults to all-clear.
            persist_path: Optional path to configs/overrides.yaml for persistence.
        """
        self.state = initial_state or OverridesConfig()
        self._persist_path = persist_path

    # ------------------------------------------------------------------
    # Strategy pause/resume
    # ------------------------------------------------------------------

    def pause_strategy(self, strategy_id: str) -> bool:
        """Pause a strategy. Returns True if state changed."""
        if strategy_id in self.state.paused_strategies:
            return False
        self.state.paused_strategies.append(strategy_id)
        self._save()
        return True

    def resume_strategy(self, strategy_id: str) -> bool:
        """Resume a paused strategy. Returns True if state changed."""
        if strategy_id not in self.state.paused_strategies:
            return False
        self.state.paused_strategies.remove(strategy_id)
        self._save()
        return True

    # ------------------------------------------------------------------
    # Symbol enable/disable
    # ------------------------------------------------------------------

    def disable_symbol(self, symbol: str) -> bool:
        """Disable a symbol. Returns True if state changed."""
        if symbol in self.state.disabled_symbols:
            return False
        self.state.disabled_symbols.append(symbol)
        self._save()
        return True

    def enable_symbol(self, symbol: str) -> bool:
        """Enable a disabled symbol. Returns True if state changed."""
        if symbol not in self.state.disabled_symbols:
            return False
        self.state.disabled_symbols.remove(symbol)
        self._save()
        return True

    # ------------------------------------------------------------------
    # Reduce-only mode
    # ------------------------------------------------------------------

    def set_reduce_only(self, enabled: bool, strategy_id: Optional[str] = None) -> bool:
        """Set reduce-only mode globally or per-strategy.

        Args:
            enabled: Whether to enable or disable reduce-only.
            strategy_id: If provided, applies to this strategy only.
                         If None, applies globally.

        Returns:
            True if state changed.
        """
        if strategy_id is not None:
            if enabled:
                if strategy_id in self.state.reduce_only_strategies:
                    return False
                self.state.reduce_only_strategies.append(strategy_id)
            else:
                if strategy_id not in self.state.reduce_only_strategies:
                    return False
                self.state.reduce_only_strategies.remove(strategy_id)
        else:
            if self.state.reduce_only == enabled:
                return False
            self.state.reduce_only = enabled
        self._save()
        return True

    # ------------------------------------------------------------------
    # System lock
    # ------------------------------------------------------------------

    def lock_system(self) -> bool:
        """Lock the system. Returns True if state changed."""
        if self.state.system_locked:
            return False
        self.state.system_locked = True
        self._save()
        return True

    def unlock_system(self) -> bool:
        """Unlock the system. Returns True if state changed."""
        if not self.state.system_locked:
            return False
        self.state.system_locked = False
        self._save()
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Persist current state to YAML if a path is configured."""
        if not self._persist_path:
            return
        try:
            data = {
                "overrides": {
                    "paused_strategies": self.state.paused_strategies,
                    "disabled_symbols": self.state.disabled_symbols,
                    "reduce_only": self.state.reduce_only,
                    "system_locked": self.state.system_locked,
                    "reduce_only_strategies": self.state.reduce_only_strategies,
                }
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        except Exception:
            logger.exception("Failed to persist overrides to %s", self._persist_path)
