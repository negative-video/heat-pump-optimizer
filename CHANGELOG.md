# Changelog

## 0.1.0

Initial release.

- Adaptive thermal model via Extended Kalman Filter (two-node RC circuit, 8-state estimation)
- Beestat temperature profile import for fast initialization
- Three-tier hierarchical control (strategic scheduling, tactical drift correction, watchdog override detection)
- Forecast-based setpoint optimization (work-based heuristic and optional LP solver)
- Counterfactual digital twin for savings tracking with decomposition (runtime, COP, rate, carbon)
- Occupancy-aware scheduling with calendar integration and pre-conditioning
- Room-aware indoor temperature weighting by occupancy
- Demand response support with temporary constraint system
- Multi-entity sensor fallback chains for resilience
- 45+ sensor entities for diagnostics, savings tracking, and model transparency
- Model export/import for backup and transfer
