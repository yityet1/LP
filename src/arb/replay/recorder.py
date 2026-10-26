"""[DATA-001] Market Data Recorder, Partitioned Storage (venue/date/symbol), and Deterministic Replay Engine."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, validator

from arb.domain.models import PriceLevel, ensure_utc
from arb.marketdata.orderbook import OrderBookBuilder


class ReplayIntegrityError(RuntimeError):
    """Raised when replay partition is missing, corrupted, or out-of-order (never silently fill missing data)."""


class RecordedMarketEvent(BaseModel):
    """Normalized market event stored in partitioned dataset (`snapshot` or `diff`)."""
    schema_version: str = "1.0.0"
    event_kind: str  # "snapshot" | "diff"
    venue_id: str
    instrument_id: str
    first_update_id: int
    final_update_id: int
    bids: List[PriceLevel] = Field(default_factory=list)
    asks: List[PriceLevel] = Field(default_factory=list)
    event_time_utc: datetime
    receive_time_utc: datetime
    monotonic_receive_ns: int = 0

    @validator("event_time_utc", "receive_time_utc", pre=True)
    def _ensure_utc_dt(cls, v: Any) -> datetime:
        if isinstance(v, str):
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            v = datetime.fromisoformat(v)
        return ensure_utc(v)


class DatasetManifest(BaseModel):
    dataset_version_id: str
    event_count: int
    partitions: List[str]
    min_event_time_utc: Optional[datetime] = None
    max_event_time_utc: Optional[datetime] = None


class DeterministicClock:
    """Fixed clock driven strictly by replayed event timestamps for 100% deterministic reproduction."""

    def __init__(self, initial_utc: Optional[datetime] = None) -> None:
        self._now_utc = ensure_utc(initial_utc) if initial_utc else datetime(2026, 1, 1, tzinfo=timezone.utc)

    @property
    def now_utc(self) -> datetime:
        return self._now_utc

    def advance_to(self, next_utc: datetime) -> None:
        ts = ensure_utc(next_utc)
        if ts < self._now_utc:
            raise ReplayIntegrityError(
                f"DeterministicClock timestamp regression: {ts.isoformat()} < {self._now_utc.isoformat()}"
            )
        self._now_utc = ts


class MarketDataRecorder:
    """
    [DATA-001] Writes and reads normalized market events partitioned by:
    `<root_dir>/venue=<venue_id>/date=<YYYY-MM-DD>/symbol=<instrument_id>/events.parquet.jsonl`
    Computes deterministic SHA-256 `dataset_version_id` across all recorded events.
    """

    def __init__(self, root_dir: Path | str) -> None:
        self._root_dir = Path(root_dir)
        self._root_dir.mkdir(parents=True, exist_ok=True)
        # In-memory mirror index so tests and high-speed replay work seamlessly even on cached 9p mounts
        self._memory_partitions: Dict[str, List[str]] = {}

    def _partition_rel_key(self, venue_id: str, date_str: str, instrument_id: str) -> str:
        return f"venue={venue_id}/date={date_str}/symbol={instrument_id}"

    def _partition_file_path(self, venue_id: str, date_str: str, instrument_id: str) -> Path:
        part_dir = (
            self._root_dir
            / f"venue={venue_id}"
            / f"date={date_str}"
            / f"symbol={instrument_id}"
        )
        part_dir.mkdir(parents=True, exist_ok=True)
        return part_dir / "part-00000.parquet.jsonl"

    def record_event(self, event: RecordedMarketEvent) -> Path:
        date_str = event.event_time_utc.strftime("%Y-%m-%d")
        rel_key = self._partition_rel_key(event.venue_id, date_str, event.instrument_id)
        file_path = self._partition_file_path(event.venue_id, date_str, event.instrument_id)
        line = event.json()
        with file_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._memory_partitions.setdefault(rel_key, []).append(line)
        return file_path

    def corrupt_partition_for_testing(self, venue_id: str, date_str: str, instrument_id: str, bad_line: str) -> None:
        rel_key = self._partition_rel_key(venue_id, date_str, instrument_id)
        file_path = self._partition_file_path(venue_id, date_str, instrument_id)
        with file_path.open("a", encoding="utf-8") as f:
            f.write(bad_line + "\n")
        self._memory_partitions.setdefault(rel_key, []).append(bad_line)

    def load_partition(
        self,
        venue_id: str,
        date_str: str,
        instrument_id: str,
    ) -> List[RecordedMarketEvent]:
        """
        Load a specific partition. Explicitly raises `ReplayIntegrityError` if the partition is missing,
        empty, or contains corrupted records (NEVER silently fill or skip missing market data).
        """
        rel_key = self._partition_rel_key(venue_id, date_str, instrument_id)
        file_path = (
            self._root_dir
            / f"venue={venue_id}"
            / f"date={date_str}"
            / f"symbol={instrument_id}"
            / "part-00000.parquet.jsonl"
        )
        raw_lines: Optional[List[str]] = None
        if rel_key in self._memory_partitions:
            raw_lines = self._memory_partitions[rel_key]
        elif file_path.exists():
            raw_lines = [ln for ln in file_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

        if not raw_lines:
            raise ReplayIntegrityError(
                f"Missing or empty market data partition: {rel_key} (silent data imputation is strictly forbidden)"
            )

        events: List[RecordedMarketEvent] = []
        for idx, line in enumerate(raw_lines, 1):
            try:
                payload = json.loads(line)
                ev = RecordedMarketEvent(**payload)
                events.append(ev)
            except Exception as exc:
                raise ReplayIntegrityError(
                    f"Corrupted market data record in partition {rel_key} at line {idx}: {exc}"
                ) from exc
        return events

    def load_dataset(
        self,
        required_partitions: List[Tuple[str, str, str]],
    ) -> Tuple[List[RecordedMarketEvent], DatasetManifest]:
        """
        Load and merge multiple `(venue_id, date_str, instrument_id)` partitions in deterministic chronological order,
        verifying per-instrument sequence monotonicity and computing a cryptographic `dataset_version_id`.
        """
        if not required_partitions:
            raise ReplayIntegrityError("required_partitions cannot be empty")

        all_events: List[RecordedMarketEvent] = []
        part_keys: List[str] = []
        for venue_id, date_str, inst_id in required_partitions:
            rel_key = self._partition_rel_key(venue_id, date_str, inst_id)
            part_keys.append(rel_key)
            part_events = self.load_partition(venue_id, date_str, inst_id)
            # Check chronological ordering inside each partition
            for i in range(len(part_events) - 1):
                if part_events[i].receive_time_utc > part_events[i + 1].receive_time_utc:
                    raise ReplayIntegrityError(
                        f"Out-of-order receive_time_utc in partition {rel_key} at index {i}"
                    )
            all_events.extend(part_events)

        # Sort deterministically by (receive_time_utc, event_time_utc, venue_id, instrument_id, final_update_id)
        all_events.sort(
            key=lambda e: (
                e.receive_time_utc,
                e.event_time_utc,
                e.venue_id,
                e.instrument_id,
                e.final_update_id,
            )
        )

        hasher = hashlib.sha256()
        for ev in all_events:
            hasher.update(ev.json(sort_keys=True).encode("utf-8"))
        dataset_version_id = f"ds_{hasher.hexdigest()[:16]}"

        manifest = DatasetManifest(
            dataset_version_id=dataset_version_id,
            event_count=len(all_events),
            partitions=sorted(part_keys),
            min_event_time_utc=all_events[0].event_time_utc if all_events else None,
            max_event_time_utc=all_events[-1].event_time_utc if all_events else None,
        )
        return all_events, manifest


class ReplayResult(BaseModel):
    dataset_version_id: str
    config_version_hash: str
    events_processed: int
    opportunities_detected: int
    executable_candidates_count: int
    rejected_candidates_count: int
    result_fingerprint: str


class DeterministicReplayEngine:
    """
    [DATA-001] Replays standardized market events into local `OrderBookBuilder` instances
    with a fixed `DeterministicClock` and invokes registered strategy scanners on each step.
    Guarantees bit-for-bit identical opportunity outputs for identical input dataset + config.
    """

    def __init__(
        self,
        config_version_hash: str,
        order_books: Dict[Tuple[str, str], OrderBookBuilder],
    ) -> None:
        self._config_version_hash = config_version_hash
        self._order_books = order_books
        self._clock = DeterministicClock()

    @property
    def clock(self) -> DeterministicClock:
        return self._clock

    def run(
        self,
        events: List[RecordedMarketEvent],
        manifest: DatasetManifest,
        on_step_callback: Optional[Callable[[RecordedMarketEvent, datetime], List[Any]]] = None,
    ) -> Tuple[ReplayResult, List[Any]]:
        if len(events) != manifest.event_count:
            raise ReplayIntegrityError(
                f"Event count mismatch: got {len(events)}, manifest expects {manifest.event_count}"
            )

        collected_opportunities: List[Any] = []
        for idx, ev in enumerate(events):
            if idx == 0:
                self._clock = DeterministicClock(ev.receive_time_utc)
            else:
                self._clock.advance_to(ev.receive_time_utc)

            key = (ev.venue_id, ev.instrument_id)
            if key not in self._order_books:
                raise ReplayIntegrityError(
                    f"Missing OrderBookBuilder for replayed instrument {ev.venue_id}:{ev.instrument_id}"
                )
            builder = self._order_books[key]

            if ev.event_kind == "snapshot":
                builder.apply_snapshot(
                    bids=ev.bids,
                    asks=ev.asks,
                    last_update_id=ev.final_update_id,
                    event_time_utc=ev.event_time_utc,
                    receive_time_utc=ev.receive_time_utc,
                )
            elif ev.event_kind == "diff":
                builder.buffer_or_apply_diff(
                    first_update_id=ev.first_update_id,
                    final_update_id=ev.final_update_id,
                    bid_deltas=ev.bids,
                    ask_deltas=ev.asks,
                    event_time_utc=ev.event_time_utc,
                    receive_time_utc=ev.receive_time_utc,
                )
            else:
                raise ReplayIntegrityError(f"Unsupported event_kind '{ev.event_kind}'")

            if on_step_callback is not None:
                step_opps = on_step_callback(ev, self._clock.now_utc)
                if step_opps:
                    collected_opportunities.extend(step_opps)

        exec_count = 0
        rej_count = 0
        fp_hasher = hashlib.sha256()
        fp_hasher.update(manifest.dataset_version_id.encode("utf-8"))
        fp_hasher.update(self._config_version_hash.encode("utf-8"))

        for opp in collected_opportunities:
            if getattr(opp, "decision", None) and getattr(opp.decision, "value", "") in ("paper", "approved"):
                exec_count += 1
            else:
                rej_count += 1
            # Hash deterministic fields (excluding random opportunity_id)
            canon = {
                "strategy_id": getattr(opp, "strategy_id", ""),
                "status": getattr(getattr(opp, "status", None), "value", ""),
                "decision": getattr(getattr(opp, "decision", None), "value", ""),
                "gross_edge": str(getattr(opp, "gross_edge", "")),
                "fees": str(getattr(opp, "fees", "")),
                "expected_net_pnl": str(getattr(opp, "expected_net_pnl", "")),
                "net_edge_bps": str(getattr(opp, "net_edge_bps", "")),
                "reject_reason_codes": getattr(opp, "reject_reason_codes", []),
            }
            fp_hasher.update(json.dumps(canon, sort_keys=True).encode("utf-8"))

        result = ReplayResult(
            dataset_version_id=manifest.dataset_version_id,
            config_version_hash=self._config_version_hash,
            events_processed=len(events),
            opportunities_detected=len(collected_opportunities),
            executable_candidates_count=exec_count,
            rejected_candidates_count=rej_count,
            result_fingerprint=fp_hasher.hexdigest()[:16],
        )
        return result, collected_opportunities
