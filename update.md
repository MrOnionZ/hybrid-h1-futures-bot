# TASK: Add a Safe, Lightweight Self-Learning and Strategy Optimization System to the Existing Trading Bot

## 1. Role and primary objective

Act as a senior Python quantitative developer specializing in algorithmic trading, backtesting, statistical validation, risk management, and low-memory ARM Linux systems.

Modify the **existing Binance USDⓈ-M Futures H1 paper-trading bot** to add a lightweight, statistically grounded self-learning system.

This is an incremental update to an existing project, not a rewrite. Preserve the existing strategy, configuration, architecture, interfaces, risk controls, order lifecycle, state persistence, and tests unless a change is necessary and explicitly justified.

The goal is to let the bot:
1. Learn from completed trades, including losing trades.
2. Identify conditions associated with poor performance.
3. Search for better strategy parameter combinations within predefined bounds.
4. Validate candidate configurations using chronological, out-of-sample testing.
5. Evaluate candidates in paper/shadow mode before promotion.
6. Automatically accept or reject candidates according to predefined criteria.
7. Roll back to a previously approved strategy when necessary.

The system must not assume that learning improves profitability. All performance claims must be supported by reproducible tests.

## 2. Existing project: inspect before changing anything

Before coding:

- Inspect the repository structure, entry point, configuration, strategy implementation, risk manager, paper broker, database/state store, backtester, logging, and tests.
- Identify the actual strategy rules and existing data schemas.
- Identify how completed trades, fees, slippage, funding, stop-losses, take-profits, and equity are recorded.
- Identify the current trading-mode safeguards.
- Reuse existing components rather than duplicating them.
- Run the existing test suite and record the baseline results.
- Create a concise implementation plan listing files to change and any required database migration.

Do not assume that files, classes, database tables, or methods exist before inspecting the repository.

Do not remove existing functionality merely to simplify the implementation. If required information is missing, implement the smallest compatible change and document it.

## 3. Target runtime and dependencies

The target device is a TV Box or ARM Linux machine with:

- 512 MB physical RAM.
- Python 3.11, subject to the existing environment's compatibility.
- CCXT for exchange market data.
- pandas and NumPy retained for numerical analysis.
- SQLite for persistent state and trade history.
- Binance USDⓈ-M Futures.
- H1 timeframe.
- Market-entry execution in the existing paper broker.
- PAPER mode enabled by default.

Keep the implementation lightweight.

Requirements:

- No GPU, neural networks, LLM API, cloud training, or machine-learning framework.
- Do not add scikit-learn, PyTorch, TensorFlow, or similar heavy dependencies.
- Use pandas and NumPy already installed in the project.
- Use Python's standard library for scheduling, statistics, configuration, and serialization wherever practical.
- Reuse the existing SQLite database and connection infrastructure.
- Run optimization sequentially with bounded memory usage.
- Never load an unlimited trade history or an entire exchange dataset into memory.
- Process historical data in bounded chunks where appropriate.
- Do not introduce multiprocessing or large numbers of concurrent tasks.
- Do not repeatedly download the same historical candles if suitable cached data already exists.
- Do not continuously write optimization diagnostics to disk; persist meaningful events and completed results.

Before adding a dependency, explain why it is necessary and whether the same functionality can be implemented with existing dependencies.

## 4. Preserve the existing trading strategy

The current strategy is a hybrid H1 strategy combining:

1. Range context and support/resistance.
2. Trendline break and break of structure (BOS).
3. SMA21/SMA50 trend alignment.
4. Retest of SMA21.
5. Candlestick confirmation.
6. Stop-loss, take-profit, and risk-based position sizing.

Preserve its current rules as the baseline strategy.

Do not silently change signal semantics, pivot confirmation, candle-close requirements, entry timing, stop placement, take-profit rules, or risk calculations during the learning-system implementation.

The optimizer may adjust only explicitly approved parameters through a configuration interface.

Examples of potentially tunable parameters include:

- SMA retest tolerance.
- Maximum number of candles to wait for a retest after BOS.
- Pivot left/right confirmation windows, within safe bounds.
- Existing range-detection tolerances.
- Existing, optional market-condition filters.

These are examples, not authorization to invent unsupported strategy rules. Only expose parameters that actually exist in the code or can be introduced with a clear justification.

Do not optimize arbitrary combinations of every parameter. Use a small, documented search space to reduce overfitting and runtime.

## 5. New modules and responsibilities

Follow the project's current architecture and naming conventions. Add separate modules only when they improve separation of concerns.

The functionality should cover these responsibilities:

### A. Trade outcome analysis

For every completed trade, store or derive:

- Trade ID and strategy version.
- Symbol and direction.
- Entry and exit timestamps.
- Entry and exit prices.
- Initial stop-loss and take-profit.
- Exit reason.
- Net PnL after modeled fees, slippage, and funding.
- Initial risk amount and realized R-multiple.
- Relevant strategy features available at entry.
- Market regime or context, if already supported by the strategy.
- Parameter version used to generate the trade.

Avoid storing redundant copies of large candle histories.

Define R-multiple consistently as net trade PnL divided by the initial monetary risk, and document any exceptions.

Never label a trade as a strategy failure solely because it lost money.

### B. Loss-pattern analysis

Analyze completed trades to identify possible performance differences across existing features, such as:

- Long versus short.
- Range versus breakout context.
- SMA21/SMA50 alignment.
- Retest distance.
- BOS and trendline-break characteristics.
- Candlestick confirmation type.
- Volatility and liquidity conditions, if reliable historical data is available.
- Exit reason and net R-multiple.

Calculate relevant statistics, including:

- Number of completed trades.
- Win rate.
- Mean and median net R-multiple.
- Expectancy per trade.
- Profit factor, with safe handling of zero gross losses.
- Maximum drawdown where equity history is available.
- Net PnL after modeled trading costs.

For each subgroup, report sample size and uncertainty. Mark small samples as inconclusive.

Do not infer causation from correlation. Do not use future data to construct features supposedly known at entry.

### C. Parameter optimizer

Implement a deterministic, reproducible optimizer over a small, bounded parameter space.

Requirements:

- Read tunable parameter definitions and limits from configuration.
- Reject invalid or out-of-range combinations.
- Limit the number of combinations evaluated per optimization cycle.
- Use fixed seeds if any randomized search is used.
- Keep the baseline configuration immutable.
- Record each candidate's parameter values, dataset range, validation results, and status.
- Prefer simple search methods such as bounded grid search when the search space is small.
- Avoid unbounded exhaustive search.
- Never modify source code to change strategy rules.
- Never optimize position sizing, leverage, daily loss limits, maximum aggregate risk, or emergency protection rules.

The optimizer must account for trading costs consistently with the existing backtester.

Do not assume that the best historical PnL or highest win rate is the best candidate.

## 6. Data separation and overfitting prevention

Implement chronological validation, not random shuffling of time-series trades or candles.

Where sufficient historical data exists, separate it into:

1. Training/optimization period.
2. Validation period.
3. Untouched out-of-sample evaluation period.

Use walk-forward validation when practical.

Requirements:

- Maintain strict chronological ordering.
- Use only finalized H1 candles.
- Respect pivot confirmation delays and indicator warm-up periods.
- Prevent look-ahead bias and future-data leakage.
- Account for overlapping trades and realistic order execution assumptions.
- Include fees, slippage, and funding costs when supported by the existing backtester.
- Handle ambiguous intrabar stop-loss/take-profit ordering conservatively.
- Ensure candidates are evaluated over comparable periods and conditions.
- Do not repeatedly tune against the same untouched out-of-sample period and continue calling it out-of-sample.

If the available dataset is too small, mark the candidate as INSUFFICIENT_DATA and do not promote it.

Do not download large datasets automatically without configuration. Reuse cached data first and enforce limits on symbols, candles, and memory usage.

## 7. Candidate evaluation and promotion

Implement a clear strategy-version lifecycle, such as:

- BASELINE
- CANDIDATE
- BACKTEST_PASSED
- PAPER_VALIDATION
- APPROVED
- REJECTED
- ROLLED_BACK

The exact implementation may differ if the project already has version management.

A candidate may be promoted only when all configured requirements are satisfied, including:

- Minimum trade count.
- Valid chronological validation.
- Acceptable out-of-sample expectancy after costs.
- Drawdown and risk constraints satisfied.
- No violation of hard safety limits.
- Performance compared with the baseline on equivalent periods.
- Successful paper/shadow validation when required.

All thresholds must be configurable and documented. Do not invent statistical certainty or claim that a particular sample size guarantees profitability.

When sample sizes are small or evidence is ambiguous, keep the baseline active.

Do not promote a candidate solely because its historical PnL is higher.

Use conservative acceptance criteria and account for multiple-testing risk when comparing many candidate parameter combinations.

## 8. Paper/shadow validation

The initial deployment must remain PAPER-only.

When a candidate passes historical validation:

1. Register it as a candidate version.
2. Evaluate it in shadow mode or a separate paper portfolio without affecting the primary paper portfolio.
3. Record hypothetical entries and exits using the same candle-close rules and modeled costs.
4. Compare it with the active baseline over comparable market periods.
5. Promote it only if the predefined validation criteria are met.
6. Otherwise reject it and preserve the baseline.

Do not simulate two strategies by allowing their trades to interfere with each other's balances or positions unless that interaction is explicitly part of the experiment.

If the existing architecture cannot safely support shadow portfolios, implement a non-trading shadow evaluator and document the limitation.

## 9. Safe rollback and runtime behavior

Persist the following in SQLite:

- Active strategy version.
- Approved baseline version.
- Candidate configurations.
- Optimization run metadata.
- Validation metrics.
- Promotion/rejection reasons.
- Rollback events.
- Relevant state required for restart recovery.

On restart, load the last approved version and reconcile persisted state before processing new signals.

Rollback triggers must be explicit and configurable. Examples include:

- Runtime errors in the new strategy version.
- Invalid or missing parameters.
- Repeated signal-generation failures.
- Configured paper-performance degradation after a sufficient evaluation window.
- Violation of a non-negotiable risk constraint.

Use hysteresis or minimum evidence requirements to prevent frequent version switching due to normal variance.

Do not roll back based on one losing trade.

A rollback must never delete historical trades or erase audit records.

## 10. Risk controls are immutable to the optimizer

The optimizer must not modify or bypass:

- Default risk per trade of 1%.
- Maximum permitted risk per trade of 2%.
- Maximum open positions.
- Aggregate open-risk limits.
- Daily loss limits.
- Maximum leverage.
- Stop-loss and take-profit protection requirements.
- Trading-mode restrictions.

Preserve the existing configured values if they differ from these examples; do not silently overwrite the user's configuration.

The learning system must not:

- Increase position size to recover losses.
- Remove or widen protective stops without explicit approval of an already supported bounded parameter.
- Disable loss limits.
- Turn on live trading.
- Create exchange orders.
- Request withdrawal permissions.
- Use API credentials for paper trading when public market data is sufficient.

Keep PAPER as the default. All live-trading paths must remain disabled unless explicitly enabled through the existing safety mechanism, and the optimizer must never enable them.

## 11. Database design and migrations

Inspect the existing schema before modifying it.

Add only the minimum required tables or columns. Potential entities include:

- Strategy versions.
- Optimization runs.
- Candidate evaluation results.
- Trade features/outcomes, if not already stored.
- Promotion and rollback audit events.

Requirements:

- Use parameterized SQL.
- Keep schema migrations backward-compatible where practical.
- Add indexes only for frequently queried fields.
- Use transactions for atomic state transitions.
- Avoid frequent full-table rewrites.
- Keep database connections and cursors properly managed.
- Handle interrupted optimization cycles without corrupting the active strategy.
- Do not duplicate trade history if existing tables already contain the required information.

Document backup and recovery behavior.

## 12. Scheduling and low-resource operation

The optimizer must not run on every market scan.

Make the following configurable:

- Optimization interval.
- Minimum number of new completed trades.
- Minimum total trade count.
- Maximum historical window.
- Maximum number of candidate combinations.
- Maximum execution time.
- Maximum symbols processed per cycle.
- Minimum free-memory threshold for starting an optimization run.

Prefer running optimization only when no new H1 candle requires immediate processing, or in a separate bounded task that cannot block the main loop.

If available memory is too low, defer the optimization run and record a concise warning rather than crashing the trading process.

Avoid unbounded queues, unbounded DataFrame accumulation, and repeated full-history copies.

The normal paper-trading loop must continue safely if optimization is unavailable or fails.

## 13. Testing requirements

Add unit, integration, regression, and failure-recovery tests.

At minimum, test:

1. Correct R-multiple and net PnL calculations.
2. Handling of missing, invalid, or incomplete trade records.
3. Correct classification of insufficient sample sizes.
4. No look-ahead bias in feature construction.
5. Chronological train/validation/test separation.
6. Reproducible candidate generation.
7. Parameter bounds enforcement.
8. Candidate rejection when costs eliminate its apparent edge.
9. Candidate rejection when drawdown exceeds configured limits.
10. No promotion when out-of-sample results are insufficient.
11. Baseline preservation when optimization fails.
12. Safe restart during an interrupted optimization.
13. Atomic promotion and rollback.
14. No change to position sizing or hard risk limits.
15. No exchange order submission from the learning module.
16. PAPER/TESTNET/LIVE isolation.
17. Memory-bounded processing with large stored histories.
18. Database migration and recovery behavior.
19. Existing strategy and order lifecycle regression tests.

Use synthetic data with known outcomes to verify statistics and validation behavior.

Tests must be deterministic and runnable without real exchange credentials or network access.

Do not claim tests passed unless they were actually executed.

## 14. Observability

Provide concise logs for:

- Optimization start and completion.
- Number of trades analyzed.
- Candidate count.
- Candidate rejection reasons.
- Baseline versus candidate metrics.
- Promotion and rollback events.
- Deferred runs due to insufficient data or low memory.
- Errors that prevent reliable evaluation.

Do not log every internal calculation or every polling iteration.

Store sufficient audit information to reproduce why a candidate was accepted or rejected.

## 15. Required deliverables

After implementation, provide:

1. Summary of the existing architecture and the changes made.
2. List of files created or modified.
3. Database migration details.
4. Explanation of the learning and promotion workflow.
5. Configuration options with defaults and safe ranges.
6. Installation/update instructions for the existing environment.
7. Commands to run unit tests and integration tests.
8. A sample optimization report clearly labeled as synthetic if it is not based on actual test results.
9. Known limitations and remaining risks.
10. Evidence of actual test execution and results.
11. A clear statement of whether the implementation was tested on ARM Linux or only on the development machine.

Do not rewrite the entire project unless inspection shows that incremental integration is genuinely impractical. If a major architectural change is necessary, explain why before making it.

## 16. Acceptance criteria

The task is complete only when:

- The original bot still runs in PAPER mode.
- The existing baseline strategy remains reproducible.
- Completed trades can be analyzed statistically.
- The optimizer searches only permitted parameters.
- Candidate evaluation is chronological and cost-aware.
- Insufficient evidence prevents automatic promotion.
- Approved versions and all decisions persist across restarts.
- Rollback restores a known approved version.
- Hard risk limits cannot be changed by the optimizer.
- The learning module cannot submit exchange orders.
- The system does not enable live trading.
- Tests cover the new functionality and existing behavior remains intact.
- Memory usage is bounded by explicit limits and optimization can be deferred under memory pressure.

Never promise profitability. The purpose of self-learning is to conduct controlled experiments and improve evidence-based decision-making, not to guarantee that future trades will win.