CREATE TABLE IF NOT EXISTS peer_comparison_reports (
    target_ticker   TEXT NOT NULL CHECK (target_ticker = upper(target_ticker)),
    fiscal_year     INTEGER NOT NULL CHECK (fiscal_year BETWEEN 1900 AND 2200),
    peer_tickers    TEXT[] NOT NULL CHECK (cardinality(peer_tickers) > 0),
    items           TEXT[] NOT NULL CHECK (cardinality(items) > 0),
    target_selection TEXT NOT NULL CHECK (target_selection IN ('material_changes', 'all')),
    run_complete    BOOLEAN NOT NULL,
    report          JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object'),
    saved_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_ticker, fiscal_year, peer_tickers, items, target_selection)
);
