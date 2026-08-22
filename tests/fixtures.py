"""Hand-built schema fixtures.

NOT real BigQuery output -- these are small stand-ins with the same SHAPE
(flat relational tables + a nested GA-style table) so schema/compaction and
guardrail logic can be exercised without credentials. Replaced by real
introspection once creds land.
"""

from src.schema import Column, SchemaContext, Table

THELOOK = Table(
    project="bigquery-public-data", dataset="thelook_ecommerce", name="order_items",
    columns=(
        Column("id", "INT64"),
        Column("order_id", "INT64"),
        Column("user_id", "INT64"),
        Column("product_id", "INT64"),
        Column("sale_price", "FLOAT64"),
        Column("created_at", "TIMESTAMP"),
        Column("status", "STRING"),
    ),
)

GA = Table(
    project="bigquery-public-data", dataset="google_analytics_sample", name="ga_sessions_20170801",
    columns=(
        Column("visitId", "INT64"),
        Column("date", "STRING"),
        Column("totals", "RECORD"),
        Column("totals.visits", "INT64"),
        Column("totals.pageviews", "INT64"),
        Column("totals.transactionRevenue", "INT64"),
        Column("trafficSource", "RECORD"),
        Column("trafficSource.source", "STRING"),
        Column("trafficSource.medium", "STRING"),
        Column("hits", "RECORD", mode="REPEATED"),
        Column("hits.product", "RECORD", mode="REPEATED"),
        Column("hits.product.productSKU", "STRING"),
    ),
)

FIXTURE = SchemaContext(tables=(THELOOK, GA))
