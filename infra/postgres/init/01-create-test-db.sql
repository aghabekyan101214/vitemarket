-- Runs once, on first init of the local Postgres volume.
-- Separate database for pytest so tests never touch dev data.
CREATE DATABASE showcase_test OWNER showcase;
