SELECT g::integer AS id, repeat('x', 64) AS payload
FROM generate_series(1, 1200000) AS g;
