"""BigQuery driver: Google Cloud SQL analytics (worker thread)."""

from __future__ import annotations

import asyncio
import time

from usr.plugins.db_hub.helpers.base import (
    DbDriver,
    DbHubError,
    ReadOnlyError,
    is_write_statement,
)

DRIVER_NAME = "bigquery"


def _project_dataset(dsn: str, options: dict) -> tuple[str, str]:
    from urllib.parse import urlparse

    parsed = urlparse((dsn or "").strip())
    if parsed.scheme != "bigquery":
        raise ValueError(
            "BigQuery dsn must look like bigquery://PROJECT/DATASET.")
    project = parsed.hostname or ""
    dataset = (parsed.path or "/").lstrip("/") or ""
    project = str(options.get("project", project) or project)
    dataset = str(options.get("dataset", dataset) or dataset)
    if not project:
        raise ValueError("BigQuery needs a project (dsn or options.project).")
    return project, dataset


class BigQueryDriver(DbDriver):
    capabilities: set[str] = {"sql"}

    def __init__(self):
        self._client = None
        self._project = ""
        self._dataset = ""
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        from google.cloud import bigquery

        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        try:
            self._project, self._dataset = _project_dataset(
                resolved_dsn, self._options)
        except ValueError as exc:
            raise DbHubError(str(exc)) from exc
        kwargs: dict = {"project": self._project}
        if self._options.get("location"):
            kwargs["location"] = self._options["location"]
        credentials_info = self._options.get("credentials_info")
        if self._options.get('oauth_access_token'):
            from google.oauth2.credentials import Credentials
            kwargs['credentials'] = Credentials(token=self._options['oauth_access_token'])
        elif isinstance(credentials_info, dict):
            from google.oauth2.service_account import Credentials

            kwargs["credentials"] = Credentials.from_service_account_info(
                credentials_info)
        elif self._options.get("credentials_json"):
            import json as _json

            from google.oauth2.service_account import Credentials

            kwargs["credentials"] = Credentials.from_service_account_info(
                _json.loads(self._options["credentials_json"]))
        self._client = await asyncio.to_thread(
            bigquery.Client, **kwargs)

    async def test(self) -> dict:
        t0 = time.monotonic()
        rows = await asyncio.to_thread(
            lambda: list(self._client.query("SELECT 1 AS ok").result()))
        return {
            "ok": bool(rows),
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "project": self._project,
            "dataset": self._dataset or "(all)",
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            if target:
                parts = target.split(".")
                if len(parts) == 3:
                    _, dataset, table = parts
                elif len(parts) == 2:
                    dataset, table = parts
                else:
                    dataset, table = self._dataset, parts[0]
                ref = self._client.dataset(dataset).table(table)
                info = self._client.get_table(ref)
                return {"table": target,
                        "columns": [{"name": f.name, "type": f.field_type,
                                     "mode": f.mode} for f in info.schema]}
            datasets = [self._dataset] if self._dataset else [
                d.dataset_id for d in self._client.list_datasets()]
            tables = {}
            for dataset in datasets:
                try:
                    tables[dataset] = [
                        t.table_id for t in self._client.list_tables(dataset)]
                except Exception:
                    tables[dataset] = []
            return {"datasets": tables,
                    "hint": "Pass target 'DATASET.TABLE' for columns."}

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        if readonly and is_write_statement(statement):
            raise ReadOnlyError("BigQuery connection is read-only.")

        def _run():
            from google.cloud.bigquery import QueryJobConfig

            job_config = None
            if params:
                job_config = QueryJobConfig(
                    query_parameters=[
                        bigquery_param(k, v)
                        for k, v in params.items()
                    ])
            rows = list(self._client.query(
                statement, job_config=job_config).result())
            dicts = [dict(r) for r in rows]
            columns = list(dicts[0].keys()) if dicts else []
            return {"columns": columns, "rows": dicts, "rowcount": len(dicts)}

        def bigquery_param(key, value):
            from google.cloud.bigquery import ScalarQueryParameter

            if isinstance(value, bool):
                return ScalarQueryParameter(key, "BOOL", value)
            if isinstance(value, int):
                return ScalarQueryParameter(key, "INT64", value)
            if isinstance(value, float):
                return ScalarQueryParameter(key, "FLOAT64", value)
            return ScalarQueryParameter(key, "STRING", str(value))

        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
            self._client = None


def create() -> BigQueryDriver:
    return BigQueryDriver()
