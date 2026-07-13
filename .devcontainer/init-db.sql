-- Runs once when the bundled Postgres initializes. The test suite gets its
-- own database so `uv run pytest` never pollutes the `tbay` database that
-- the demo and the dashboard share.
CREATE DATABASE tbay_test;
