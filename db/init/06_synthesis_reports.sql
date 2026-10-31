CREATE TABLE IF NOT EXISTS synthesis_reports (
    company       TEXT NOT NULL CHECK (company = upper(company)),
    years         INTEGER[] NOT NULL CHECK (cardinality(years) >= 2),
    report        JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object'),
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company, years)
);
