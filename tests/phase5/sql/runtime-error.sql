SELECT g::integer AS id, (100 / (100 - g))::integer AS value
FROM generate_series(1, 200) AS g;
