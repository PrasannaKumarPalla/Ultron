"""Telemetry â€” SQLite-backed inference recording and instrumented wrappers."""

from __future__ import annotations

from bujji.telemetry.aggregator import (
    AggregatedStats,
    EngineStats,
    ModelStats,
    TelemetryAggregator,
)
from bujji.telemetry.store import TelemetryStore
from bujji.telemetry.wrapper import instrumented_generate

try:
    from bujji.telemetry.gpu_monitor import (
        GpuHardwareSpec,
        GpuMonitor,
        GpuSample,
        GpuSnapshot,
    )
except ImportError:
    pass

try:
    from bujji.telemetry.efficiency import EfficiencyMetrics, compute_efficiency
except ImportError:
    pass

try:
    from bujji.telemetry.vllm_metrics import VLLMMetrics, VLLMMetricsScraper
except ImportError:
    pass

try:
    from bujji.telemetry.energy_monitor import (
        EnergyMonitor,
        EnergySample,
        EnergyVendor,
        create_energy_monitor,
    )
except ImportError:
    pass

from bujji.telemetry.batch import BatchMetrics, EnergyBatch
from bujji.telemetry.steady_state import (
    SteadyStateConfig,
    SteadyStateDetector,
    SteadyStateResult,
)

try:
    from bujji.telemetry.session import TelemetrySample, TelemetrySession
except ImportError:
    pass

try:
    from bujji.telemetry.phase_metrics import compute_phase_metrics, split_at_ttft
except ImportError:
    pass

try:
    from bujji.telemetry.itl import compute_itl_stats
except ImportError:
    pass

try:
    from bujji.telemetry.flops import (
        GPU_PEAK_TFLOPS_BF16,
        MODEL_PARAMS_B,
        compute_mfu,
        estimate_flops,
        estimate_flops_no_kv_cache,
    )
except ImportError:
    pass

__all__ = [
    "AggregatedStats",
    "BatchMetrics",
    "EfficiencyMetrics",
    "EnergyBatch",
    "EnergyMonitor",
    "EnergySample",
    "EnergyVendor",
    "EngineStats",
    "GpuHardwareSpec",
    "GpuMonitor",
    "GpuSample",
    "GpuSnapshot",
    "ModelStats",
    "TelemetryAggregator",
    "TelemetryStore",
    "VLLMMetrics",
    "VLLMMetricsScraper",
    "SteadyStateConfig",
    "SteadyStateDetector",
    "SteadyStateResult",
    "TelemetrySession",
    "TelemetrySample",
    "compute_phase_metrics",
    "split_at_ttft",
    "compute_itl_stats",
    "estimate_flops",
    "estimate_flops_no_kv_cache",
    "compute_mfu",
    "GPU_PEAK_TFLOPS_BF16",
    "MODEL_PARAMS_B",
    "compute_efficiency",
    "create_energy_monitor",
    "instrumented_generate",
]
