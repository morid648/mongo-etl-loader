-- Synthetic seed data for local dev/testing of the SQL extractor (PRD §11, task 2.3.2).
-- Postgres-compatible; run against the local docker-compose Postgres or any scratch DB.

CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    email VARCHAR(255),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

INSERT INTO customers (first_name, last_name, email, updated_at) VALUES
    ('Alice', 'Nguyen', 'alice.nguyen@example.com', '2024-01-01 09:00:00'),
    ('Bob', 'Martinez', 'bob.martinez@example.com', '2024-01-02 10:15:00'),
    ('Carol', 'Chen', 'carol.chen@example.com', '2024-01-03 11:30:00'),
    ('Dave', 'Okafor', 'dave.okafor@example.com', '2024-01-04 12:45:00'),
    ('Eve', 'Larsen', NULL, '2024-01-05 14:00:00');
