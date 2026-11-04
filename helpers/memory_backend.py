"""Database-backed agent memory for db_hub.

When `memory_backend.enabled` names a vector-capable connection, the manual
memory tools (memory_save/load/delete/forget) and the recall/memorize
extensions route here instead of the built-in FAISS store. Everything else —
recall gates, thresholds, prompts, consolidation settings — keeps working
because the router honors the `_memory` plugin's config and output contracts.

Score parity: every vector store speaks a different score language, so raw
scores are normalized to 0..1 cosine-like similarity before the caller's
threshold applies. Per-connection `options.score_hint` overrides the driver's
default assumption.
"""

from __future__ import annotations

import asyncio
import re
import time
import hashlib

from helpers import plugins

from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

PLUGIN = "db_hub"
AREAS = ("main", "fragments", "solutions")
_AREA_FILTER_RE = re.compile(r"area\s*==\s*['\"]([a-zA-Z0-9_]+)['\"]")


def _memory_cfg(agent=None, project_name: str = "", agent_profile: str = "") -> dict:
    if agent is not None:
        data = plugins.get_plugin_config(PLUGIN, agent=agent) or {}
    else:
        data = plugins.get_plugin_config(
            PLUGIN, project_name=project_name,
            agent_profile=agent_profile) or {}
    from usr.plugins.db_hub.helpers import config as config_helper

    return config_helper.normalize_for_read(data)


def backend_for(agent=None, project_name: str = "",
                agent_profile: str = "") -> dict | None:
    """Legacy single-backend view; prefer backend_targets for new code."""
    targets = backend_targets(agent, project_name, agent_profile)
    if not targets:
        return None
    return {"connection": targets[0]["connection"],
            "prefix": targets[0]["prefix"]}


def backend_targets(agent=None, project_name: str = "",
                    agent_profile: str = "") -> list[dict]:
    """Active memory backend targets [{connection, prefix}] (possibly many)."""
    cfg = _memory_cfg(agent, project_name, agent_profile)
    backend = cfg.get("memory_backend", {}) or {}
    if not backend.get("enabled"):
        return []
    prefix = backend.get("prefix", "mem") or "mem"
    targets = []
    for item in backend.get("connections", []) or []:
        name = item.get("connection", "") if isinstance(item, dict) else ""
        if name and all(t["connection"] != name for t in targets):
            targets.append({"connection": name, "prefix": prefix})
    if not targets:
        raise DbHubError("Database memory is enabled but no connection is selected.")
    return targets


def backend_enabled(agent=None) -> bool:
    return bool(_memory_cfg(agent).get("memory_backend", {}).get("enabled"))


def scoped_prefix(agent, prefix: str, scope=None) -> str:
    project, profile = scope if scope is not None else registry.scope_key(agent)
    subdir = resolve_scope_subdir(project, profile)
    if subdir == "default":
        return prefix
    # Keep the existing default-store names; isolate project/custom stores using
    # the same memory-subdirectory contract as built-in memory.
    return prefix + "_" + hashlib.sha256(subdir.encode()).hexdigest()[:12]


def _failure(agent, operation: str, errors: list[str]) -> str:
    from usr.plugins.db_hub.helpers import secrets_bridge
    return secrets_bridge.mask_text(
        f"Database memory {operation} failed: " + "; ".join(errors),
        context=getattr(agent, "context", None))


async def _call(bound, method, *args, write=False, **kwargs):
    if write and registry.effective_readonly(bound.conn_cfg, bound.glob, None):
        raise DbHubError(f"Memory connection '{bound.conn_cfg['id']}' must allow writes.")
    try:
        await bound._touch()
        return await asyncio.wait_for(method(*args, **kwargs), bound._timeout())
    except Exception as exc:
        raise DbHubError(bound.mask(f"{type(exc).__name__}: {exc}")) from exc


def _dedup_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def collection_for(prefix: str, area: str) -> str:
    return f"{prefix}_{area}"


def areas_for_filter(filter_text: str) -> list[str]:
    """Narrow areas from a `_memory`-style filter expression, if it names one."""
    if not filter_text:
        return list(AREAS)
    found = _AREA_FILTER_RE.findall(filter_text or "")
    picked = [a for a in found if a in AREAS]
    return picked or list(AREAS)


def to_similarity(score, hint: str) -> float | None:
    """Normalize a raw vector score to 0..1 cosine-like similarity."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if hint == "cosine_distance":
        return max(0.0, min(1.0, 1.0 - value))
    if hint == "l2_distance":
        return max(0.0, min(1.0, 1.0 / (1.0 + max(0.0, value))))
    return max(0.0, min(1.0, value))


async def _embed(agent, text: str) -> list[float]:
    model = agent.get_embedding_model() if agent is not None else None
    if model is None:
        if agent is None:
            from plugins._model_config.helpers.model_config import (
                get_embedding_model_config_object,
            )
            import models as models_helper

            config = get_embedding_model_config_object()
            model = models_helper.get_embedding_model(
                config.provider, config.name, **config.build_kwargs())
        else:
            raise DbHubError(
                "No embedding model is configured; memory needs one.")
    for attr in ("embed_query", "aembed_query"):
        fn = getattr(model, attr, None)
        if callable(fn):
            vector = fn(text)
            if hasattr(vector, "__await__"):
                vector = await vector
            return [float(x) for x in vector]
    raise DbHubError("The configured embedding model cannot embed text.")


async def _bound(agent, connection: str, scope: tuple[str, str] | None = None):
    from usr.plugins.db_hub.helpers import drivers as driver_registry

    if scope:
        bound = await registry.get_connection(
            connection, agent=None, project_name=scope[0], agent_profile=scope[1])
    else:
        bound = await registry.get_connection(connection, agent=agent)
    info = driver_registry.get_driver_info(bound.conn_cfg["driver"])
    if "vector" not in info["capabilities"] or not isinstance(
            bound.driver, VectorDriver):
        raise DbHubError(
            f"Memory backend connection '{connection}' "
            f"({bound.conn_cfg['driver']}) is not vector-capable. "
            "Pick a supported memory store: Postgres+pgvector (including Neon/Supabase), "
            "Qdrant, LanceDB, ChromaDB, or prepared SpacetimeDB.")
    return bound, info


def _score_hint(bound, info: dict) -> str:
    options = bound.conn_cfg.get("options", {}) or {}
    hint = str(options.get("score_hint", "") or info.get("score", "") or "")
    if hint in {"cosine_similarity", "cosine_distance", "l2_distance"}:
        return hint
    return "cosine_similarity"


async def _ensure_collection(bound, collection: str, dim: int) -> None:
    driver = bound.driver
    ensure = getattr(driver, "ensure_collection", None)
    if not callable(ensure):
        raise DbHubError(
            f"Driver '{bound.conn_cfg['driver']}' cannot create collections "
            "itself. Create collection/table/index "
            f"'{collection}' (vector dim {dim}) manually, then retry.")
    await ensure(collection, dim)


def _new_id(area: str) -> str:
    from helpers import guids

    return f"mem_{area}_{guids.generate_id(10)}"


def _split_id(memory_id: str) -> tuple[str | None, str]:
    match = re.fullmatch(r"mem_([a-z0-9_]+)_([A-Za-z0-9]+)", memory_id or "")
    if match:
        return match.group(1), memory_id
    return None, memory_id


def _payload(area: str, text: str, metadata: dict, memory_id: str) -> dict:
    """Fixed-shape payload: every driver stores and returns the same keys."""
    import json as _json

    extras = {}
    for key, value in (metadata or {}).items():
        if key not in {"text", "area", "timestamp", "memory_id", "meta"} and isinstance(
                value, (str, int, float, bool)):
            extras[key] = value
    return {"text": text, "area": area, "timestamp": _now_iso(),
            "memory_id": memory_id, "meta": _json.dumps(extras)}


def _now_iso() -> str:
    from helpers.localization import Localization

    return Localization.get().now_iso(timespec="seconds")


def _to_documents(matches: list[dict]):
    import json as _json

    from langchain_core.documents import Document

    docs = []
    for match in matches:
        match = dict(match)
        match.pop("_distance", None)
        score = match.pop("_score", None)
        text = str(match.pop("text", "") or "")
        metadata = {
            "area": str(match.pop("area", "") or ""),
            "timestamp": str(match.pop("timestamp", "") or ""),
            "memory_id": str(match.pop("memory_id", "") or ""),
        }
        metadata["id"] = str(match.get("id", ""))
        meta_raw = match.pop("meta", None)
        if isinstance(meta_raw, str) and meta_raw:
            try:
                extras = _json.loads(meta_raw)
                if isinstance(extras, dict):
                    metadata.update({k: v for k, v in extras.items()
                                     if isinstance(v, (str, int, float, bool))})
            except ValueError:
                pass
        for key, value in match.items():
            if key not in metadata and isinstance(value, (str, int, float, bool)):
                metadata[key] = value
        metadata["score"] = score
        docs.append(Document(page_content=text, metadata=metadata))
    return docs


async def memory_save(agent, text: str, area: str = "main",
                      metadata: dict | None = None,
                      connection: str = "", prefix: str = "mem",
                      scope: tuple[str, str] | None = None) -> str:
    """Save one memory; returns its id. Mirrors Memory.insert_text."""
    area = (area or "main").lower()
    if area not in AREAS:
        area = "main"
    if agent is not None and not connection:
        targets = backend_targets(agent)
        if not targets:
            raise DbHubError("No memory backend connection configured.")
        memory_id = _new_id(area)
        saved = 0
        errors: list[str] = []
        for target in targets:
            try:
                await _save_to_target(agent, memory_id, text, area, metadata,
                                      target["connection"], target["prefix"],
                                      scope)
                saved += 1
            except Exception as exc:
                errors.append(f"{target['connection']}: {exc}")
        if errors:
            raise DbHubError(_failure(agent, "save", [
                f"{saved}/{len(targets)} stores saved memory {memory_id}; "
                "replication is incomplete", *errors]))
        return memory_id
    if not connection:
        raise DbHubError("No memory backend connection configured.")
    memory_id = _new_id(area)
    await _save_to_target(agent, memory_id, text, area, metadata,
                          connection, prefix, scope)
    return memory_id


async def _save_to_target(agent, memory_id: str, text: str, area: str,
                          metadata: dict | None, connection: str,
                          prefix: str,
                          scope: tuple[str, str] | None) -> None:
    bound, info = await _bound(agent, connection, scope)
    vector = await _embed(agent, text)
    collection = collection_for(scoped_prefix(agent, prefix, scope), area)
    await _call(bound, _ensure_collection, bound, collection, len(vector), write=True)
    await _call(bound, bound.driver.vector_upsert, collection, [{
        "id": memory_id, "vector": vector,
        "payload": _payload(area, text, metadata, memory_id)}], write=True)
    await registry.evict_idle()


async def memory_search(agent, query: str, threshold: float = 0.7,
                        limit: int = 10, areas: tuple[str, ...] = AREAS,
                        filter_text: str = "", connection: str = "",
                        prefix: str = "mem",
                        scope: tuple[str, str] | None = None,
                        _vector: list[float] | None = None) -> list[dict]:
    """Vector-search memories; returns [{id, doc, score}] sorted by score."""
    if agent is not None and not connection:
        targets = backend_targets(agent)
        if not targets:
            raise DbHubError("No memory backend connection configured.")
        merged: list[dict] = []
        seen_ids: set[str] = set()
        seen_texts: set[str] = set()
        errors = []
        succeeded = 0
        if _vector is None:
            _vector = await _embed(agent, query)
        for target in targets:
            try:
                found = await memory_search(
                    agent, query, threshold, limit, areas, filter_text,
                    target["connection"], target["prefix"], scope,
                    _vector=_vector)
                succeeded += 1
            except Exception as exc:
                errors.append(f"{target['connection']}: {exc}")
                continue
            for item in found:
                key = _dedup_key(item["doc"].page_content)
                if item["id"] in seen_ids or (key and key in seen_texts):
                    continue
                seen_ids.add(item["id"])
                if key:
                    seen_texts.add(key)
                merged.append(item)
        merged.sort(key=lambda item: item["score"], reverse=True)
        if errors:
            message = _failure(agent, "recall", errors)
            if not succeeded:
                raise DbHubError(message)
            if getattr(getattr(agent, "context", None), "log", None):
                agent.context.log.log(type="warning", heading="Database memory recall incomplete", content=message)
        return merged[:max(int(limit or 10), 1)]
    if not connection:
        raise DbHubError("No memory backend connection configured.")
    bound, info = await _bound(agent, connection, scope)
    hint = _score_hint(bound, info)
    vector = _vector if _vector is not None else await _embed(agent, query)
    per_area = max(int(limit or 10) * 2 + 5, 10)
    scored: list[dict] = []
    seen: set[str] = set()
    prefix = scoped_prefix(agent, prefix, scope)
    from plugins._memory.helpers.memory import Memory as BundledMemory
    comparator = BundledMemory._get_comparator(filter_text) if filter_text else None
    for area in areas:
        if area not in AREAS:
            continue
        try:
            result = await _call(bound, bound.driver.vector_search,
                collection_for(prefix, area), vector, per_area, None)
        except DbHubError as exc:
            if "does not exist" in str(exc) or "not found" in str(exc).lower():
                continue
            raise
        for match in result.get("matches", []) or []:
            memory_id = str(match.get("id", ""))
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            similarity = to_similarity(
                match.get("_score", match.get("_distance", match.get("score"))), hint)
            if similarity is None or similarity < float(threshold):
                continue
            docs = _to_documents([{**match, "_score": similarity}])
            if comparator and not comparator(docs[0].metadata):
                continue
            scored.append({"id": memory_id, "doc": docs[0], "score": similarity})
    scored.sort(key=lambda item: item["score"], reverse=True)
    await registry.evict_idle()
    return scored[:max(int(limit or 10), 1)]


async def memory_delete(agent, ids: list[str], connection: str = "",
                        prefix: str = "mem",
                        scope: tuple[str, str] | None = None) -> int:
    """Delete memories by id; returns the deleted count."""
    if agent is not None and not connection:
        targets = backend_targets(agent)
        if not targets:
            raise DbHubError("No memory backend connection configured.")
        total = 0
        errors = []
        for target in targets:
            try:
                total += await memory_delete(
                    agent, ids, target["connection"], target["prefix"], scope)
            except Exception as exc:
                errors.append(f"{target['connection']}: {exc}")
        if errors:
            raise DbHubError(_failure(agent, "delete", errors))
        return total
    if not connection:
        raise DbHubError("No memory backend connection configured.")
    bound, _ = await _bound(agent, connection, scope)
    prefix = scoped_prefix(agent, prefix, scope)
    by_collection: dict[str, list[str]] = {}
    for memory_id in ids or []:
        area, _ = _split_id(str(memory_id))
        if area in AREAS:
            by_collection.setdefault(collection_for(prefix, area), []).append(
                str(memory_id))
        else:
            for area_name in AREAS:
                by_collection.setdefault(
                    collection_for(prefix, area_name), []).append(str(memory_id))
    deleted = 0
    for collection, id_list in by_collection.items():
        try:
            result = await _call(bound, bound.driver.vector_delete,
                collection, ids=id_list, flt=None, write=True)
        except DbHubError as exc:
            if "does not exist" in str(exc) or "not found" in str(exc).lower():
                continue
            raise
        count = result.get("deleted", 0)
        deleted += count if isinstance(count, int) else len(id_list)
    await registry.evict_idle()
    return deleted


async def memory_forget(agent, query: str, threshold: float = 0.7,
                        filter_text: str = "", connection: str = "",
                        prefix: str = "mem", limit: int = 100,
                        scope: tuple[str, str] | None = None) -> list[str]:
    """Find and remove memories matching a query; returns removed ids."""
    areas = areas_for_filter(filter_text)
    found = await memory_search(agent, query, threshold, limit, tuple(areas),
                                filter_text, connection, prefix, scope)
    removed = [item["id"] for item in found]
    if removed:
        await memory_delete(agent, removed, connection, prefix, scope)
    return removed


def resolve_scope_subdir(project_name: str = "", agent_profile: str = "") -> str:
    """Replicate _memory's per-scope subdirectory resolution without an agent."""
    mem_cfg = plugins.get_plugin_config(
        "_memory", project_name=project_name,
        agent_profile=agent_profile) or {}
    if mem_cfg.get("project_memory_isolation", True) and project_name:
        return "projects/" + project_name
    return mem_cfg.get("agent_memory_subdir", "") or "default"


async def migrate_scope(project_name: str = "", agent_profile: str = "",
                        connection: str = "", prefix: str = "mem",
                        areas: tuple[str, ...] = AREAS, limit: int = 500,
                        progress=None) -> dict:
    """Copy FAISS memories of one scope into the DB backend. Read-only on FAISS."""
    from plugins._memory.helpers.memory import Memory as BundledMemory

    if not connection:
        raise DbHubError("Pick a memory backend connection first.")
    if not prefix:
        mapping = backend_for(None, project_name, agent_profile)
        prefix = (mapping or {}).get("prefix", "") or "mem"
    subdir = resolve_scope_subdir(project_name, agent_profile)
    db = await BundledMemory.get_by_subdir(subdir, preload_knowledge=False)
    docs = db.get_all_docs() or {}
    moved, skipped = 0, 0
    for doc_id, doc in docs.items():
        if moved >= max(int(limit or 500), 1):
            break
        area = str((doc.metadata or {}).get("area", "main") or "main").lower()
        if area not in areas:
            continue
        text = getattr(doc, "page_content", "") or ""
        if not text.strip():
            skipped += 1
            continue
        metadata = {k: v for k, v in (doc.metadata or {}).items()
                    if k not in {"area", "id"}}
        try:
            await memory_save(None, text, area, metadata, connection, prefix,
                              scope=(project_name, agent_profile))
            moved += 1
        except Exception:
            skipped += 1
        if progress and moved % 25 == 0:
            await progress(moved)
    await registry.evict_idle()
    return {"moved": moved, "skipped": skipped, "scope": subdir}


async def route_memory_tool(agent, op: str, params: dict):
    """Implement a memory tool call on the DB backend, or None to delegate."""
    targets = backend_targets(agent)
    if not targets:
        if "_memory" not in plugins.get_enabled_plugins(agent):
            from helpers.tool import Response
            return Response(message="Memory is disabled. Enable built-in Memory or select a Database Hub memory backend.", break_loop=False)
        return None
    from helpers.tool import Response

    try:
        await _bound(agent, targets[0]["connection"])
    except DbHubError as exc:
        return Response(
            message=f"Database memory backend failed ({exc}). "
            "Disable it in Database Hub settings to fall back to built-in memory.",
            break_loop=False)

    await agent.handle_intervention(f"db_hub: memory {op}")
    try:
        if op == "save":
            area = str(params.get("area", "") or "main")
            metadata = {k: v for k, v in params.items()
                        if k not in {"text", "area"}}
            memory_id = await memory_save(agent, params.get("text", ""),
                                          area, metadata)
            message = agent.read_prompt("fw.memory_saved.md", memory_id=memory_id)
            return Response(message=message, break_loop=False)
        if op == "load":
            threshold = float(params.get("threshold", 0.7))
            limit = int(params.get("limit", 10) or 10)
            areas = areas_for_filter(str(params.get("filter", "") or ""))
            found = await memory_search(
                agent, params.get("query", ""), threshold, limit,
                tuple(areas), str(params.get("filter", "") or ""))
            if not found:
                message = agent.read_prompt(
                    "fw.memories_not_found.md", query=params.get("query", ""))
            else:
                from plugins._memory.helpers.memory import (
                    Memory as BundledMemory,
                )

                message = str("\n\n".join(BundledMemory.format_docs_plain(
                    [item["doc"] for item in found])))
            return Response(message=message, break_loop=False)
        if op == "delete":
            ids = [part.strip() for part in str(params.get("ids", "")).split(",")
                   if part.strip()]
            count = await memory_delete(agent, ids)
            message = agent.read_prompt("fw.memories_deleted.md",
                                        memory_count=count)
            return Response(message=message, break_loop=False)
        if op == "forget":
            threshold = float(params.get("threshold", 0.7))
            removed = await memory_forget(
                agent, params.get("query", ""), threshold,
                str(params.get("filter", "") or ""))
            message = agent.read_prompt("fw.memories_deleted.md",
                                        memory_count=len(removed))
            return Response(message=message, break_loop=False)
    except Exception as exc:
        return Response(
            message=_failure(agent, op, [str(exc)]),
            break_loop=False)
    return None
