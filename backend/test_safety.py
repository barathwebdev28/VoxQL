from query_engine import validate_sql


test_queries = [
    # Should pass
    "SELECT * FROM orders;",
    """
    SELECT c.customer_name, r.region_name
    FROM customers c
    JOIN regions r ON r.region_id = c.region_id;
    """,
    """
    WITH recent AS (
        SELECT * FROM orders
    )
    SELECT * FROM recent;
    """,
    "SELECT LOWER(status) FROM orders;",
    "SELECT COUNT(*) FROM orders;",
    "SELECT SUM(unit_price) FROM products;",
    "SELECT CURRENT_DATE;",

    # Should be blocked
    "SELECT * FROM pg_catalog.pg_tables;",
    "SELECT * FROM information_schema.tables;",
    "SELECT * FROM payments;",
    "SELECT * FROM orders; DROP TABLE orders;",
    "DELETE FROM orders;",
    "UPDATE products SET unit_price = 0;",
    "DROP TABLE customers;",
    """
    WITH deleted AS (
        DELETE FROM orders
        RETURNING *
    )
    SELECT * FROM deleted;
    """,
    "SELECT pg_sleep(10);",
    "SELECT set_config('x', 'y', false);",
    "SELECT pg_read_file('/etc/passwd');",
    "SELECT dblink_connect('remote_connection');",
    "SELECT pg_terminate_backend(123);",
    "SELECT nextval('orders_order_id_seq');",
]


for query in test_queries:
    is_safe, reason = validate_sql(query)

    result = "SAFE" if is_safe else "BLOCKED"

    print("-" * 80)
    print(result)
    print(query.strip())
    print(reason)