"""Thin BigQuery helpers shared by all quant data loaders."""

import pandas as pd
from google.cloud import bigquery

from quant.config import BQ_DATASET, BQ_LOCATION, GCP_PROJECT

_client = None


def client() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=GCP_PROJECT, location=BQ_LOCATION)
    return _client


def query(sql: str) -> pd.DataFrame:
    return client().query(sql).result().to_dataframe()


def scalar(sql: str):
    rows = list(client().query(sql).result())
    return rows[0][0] if rows else None


def load_df(table_id: str, df: pd.DataFrame, schema=None, write="WRITE_APPEND"):
    """Load a DataFrame via a load job (not streaming — cheaper and atomic)."""
    job_config = bigquery.LoadJobConfig(write_disposition=write)
    if schema:
        job_config.schema = schema
    job = client().load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    return job


def ensure_table(table_id: str, schema, partition_field=None, clustering=None,
                 partition_granularity="MONTH"):
    """Create a table with partitioning/clustering if it doesn't exist."""
    from google.cloud.exceptions import NotFound

    try:
        return client().get_table(table_id)
    except NotFound:
        table = bigquery.Table(table_id, schema=schema)
        if partition_field:
            table.time_partitioning = bigquery.TimePartitioning(
                type_=getattr(bigquery.TimePartitioningType, partition_granularity),
                field=partition_field,
            )
        if clustering:
            table.clustering_fields = clustering
        return client().create_table(table)
