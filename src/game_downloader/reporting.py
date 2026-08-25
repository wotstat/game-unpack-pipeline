from __future__ import annotations

from collections import Counter

from game_downloader._json import JsonObject, JsonValue
from game_downloader.models import Stage


def _object(value: JsonValue | None) -> JsonObject | None:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return None


def _items(value: JsonValue | None) -> list[JsonValue] | None:
    return value if isinstance(value, list) else None


def _integer(value: JsonValue | None) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _number(value: JsonValue | None) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _objects(values: list[JsonValue]) -> tuple[JsonObject, ...]:
    return tuple(item for value in values if (item := _object(value)) is not None)


def _with_rate(
    statistics: JsonObject,
    *,
    numerator: int,
    duration_seconds: float | None,
    key: str = "bytes_per_second",
) -> JsonObject:
    if duration_seconds is not None and duration_seconds > 0:
        statistics[key] = round(numerator / duration_seconds, 3)
    return statistics


def _resolve_statistics(payload: JsonObject) -> JsonObject:
    parts = _items(payload.get("version_vector"))
    responses = _items(payload.get("raw_responses"))
    if parts is None or responses is None:
        return {}
    response_objects = _objects(responses)
    return {
        "parts": len(parts),
        "protocol_responses": len(responses),
        "protocol_response_bytes": sum(
            len(raw_xml.encode("utf-8"))
            for response in response_objects
            if (raw_xml := _string(response.get("raw_xml"))) is not None
        ),
    }


def _plan_statistics(payload: JsonObject) -> JsonObject:
    parts = _items(payload.get("parts"))
    descriptors = _items(payload.get("descriptors"))
    disk_space = _object(payload.get("disk_space"))
    if parts is None or descriptors is None or disk_space is None:
        return {}
    artifact_count = 0
    for part in _objects(parts):
        artifacts = _items(part.get("artifacts"))
        if artifacts is not None:
            artifact_count += len(artifacts)
    return {
        "parts": len(parts),
        "artifacts": artifact_count,
        "torrent_descriptors": len(descriptors),
        "descriptor_bytes": _integer(disk_space.get("descriptor_bytes")),
        "planned_download_bytes": _integer(disk_space.get("download_bytes")),
        "planned_assembled_bytes": _integer(disk_space.get("assembled_bytes")),
        "required_free_bytes": _integer(disk_space.get("required_free_bytes")),
    }


def _download_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_artifacts = _items(payload.get("artifacts"))
    if raw_artifacts is None:
        return {}
    artifacts = _objects(raw_artifacts)
    reused_artifacts = 0
    reused_bytes = 0
    resumed_bytes = 0
    network_bytes_estimate = 0
    download_attempts = 0
    parallel_range_artifacts = 0
    parallel_range_segments = 0
    methods: Counter[str] = Counter()
    for item in artifacts:
        blob_size = _integer(item.get("blob_size"))
        if item.get("reused") is True:
            reused_artifacts += 1
            reused_bytes += blob_size
            continue
        transport = _object(item.get("transport")) or {}
        resumed = min(blob_size, _integer(transport.get("resumed_from")))
        resumed_bytes += resumed
        network_bytes_estimate += blob_size - resumed
        download_attempts += _integer(transport.get("attempts"))
        segments = max(1, _integer(transport.get("parallel_segments")))
        if segments > 1:
            parallel_range_artifacts += 1
            parallel_range_segments += segments
        method = _string(transport.get("method"))
        if method is not None:
            methods[method] += 1
    statistics: JsonObject = {
        "artifacts": len(raw_artifacts),
        "payload_bytes": _integer(payload.get("downloaded_bytes")),
        "fetched_artifacts": len(raw_artifacts) - reused_artifacts,
        "network_bytes_estimate": network_bytes_estimate,
        "reused_artifacts": reused_artifacts,
        "reused_bytes": reused_bytes,
        "resumed_bytes": resumed_bytes,
        "download_attempts": download_attempts,
        "parallel_range_artifacts": parallel_range_artifacts,
        "parallel_range_segments": parallel_range_segments,
        "web_seed_artifacts": methods["web-seed"],
        "torrent_artifacts": methods["torrent"],
    }
    return _with_rate(
        statistics,
        numerator=network_bytes_estimate,
        duration_seconds=duration_seconds,
        key="network_bytes_per_second",
    )


def _verify_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_artifacts = _items(payload.get("artifacts"))
    raw_assemblies = _items(payload.get("split_assemblies"))
    if raw_artifacts is None or raw_assemblies is None:
        return {}
    artifacts = _objects(raw_artifacts)
    assemblies = _objects(raw_assemblies)
    checked_bytes = 0
    archive_entries = 0
    for item in artifacts:
        download = _object(item.get("download")) or {}
        checked_bytes += _integer(download.get("blob_size"))
        archive_entries += _integer(item.get("entries"))
    statistics: JsonObject = {
        "artifacts": len(raw_artifacts),
        "checked_bytes": checked_bytes,
        "archive_entries": archive_entries,
        "split_assemblies": len(raw_assemblies),
        "split_assembly_bytes": sum(_integer(item.get("blob_size")) for item in assemblies),
    }
    return _with_rate(
        statistics,
        numerator=checked_bytes,
        duration_seconds=duration_seconds,
    )


def _assemble_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_files = _items(payload.get("files"))
    if raw_files is None:
        return {}
    files = _objects(raw_files)
    output_bytes = sum(_integer(item.get("blob_size")) for item in files)
    methods = Counter(_string(item.get("link_method")) for item in files)
    statistics: JsonObject = {
        "files": len(raw_files),
        "output_bytes": output_bytes,
        "hardlinked_files": methods["hardlink"],
        "copied_files": methods["copy"],
    }
    return _with_rate(statistics, numerator=output_bytes, duration_seconds=duration_seconds)


def _index_statistics(payload: JsonObject) -> JsonObject:
    raw_packages = _items(payload.get("packages"))
    raw_entries = _items(payload.get("entries"))
    if raw_packages is None or raw_entries is None:
        return {}
    packages = _objects(raw_packages)
    entries = _objects(raw_entries)
    candidate_counts = [
        len(candidates)
        for item in entries
        if (candidates := _items(item.get("candidates"))) is not None
    ]
    return {
        "packages": len(raw_packages),
        "package_bytes": sum(_integer(item.get("blob_size")) for item in packages),
        "package_entries": sum(_integer(item.get("entries")) for item in packages),
        "indexed_files": len(raw_entries),
        "candidates": sum(candidate_counts),
        "conflicts": sum(count > 1 for count in candidate_counts),
    }


def _materialize_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_files = _items(payload.get("files"))
    if raw_files is None:
        return {}
    files = _objects(raw_files)
    output_bytes = sum(_integer(item.get("size")) for item in files)
    sources: Counter[str] = Counter()
    for item in files:
        source = _object(item.get("source")) or {}
        source_kind = _string(source.get("source_kind"))
        if source_kind is not None:
            sources[source_kind] += 1
    statistics: JsonObject = {
        "files": len(raw_files),
        "output_bytes": output_bytes,
        "package_files": sources["game-package"],
        "loose_files": sources["loose-file"],
    }
    return _with_rate(statistics, numerator=output_bytes, duration_seconds=duration_seconds)


def _readable_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_files = _items(payload.get("files"))
    raw_actionscript = _items(payload.get("actionscript_files"))
    raw_stubs = _items(payload.get("stub_files"))
    if raw_files is None or raw_actionscript is None or raw_stubs is None:
        return {}
    files = _objects(raw_files + raw_actionscript)
    representations: Counter[str] = Counter()
    diagnostics = 0
    for item in files:
        representation = _object(item.get("representation")) or {}
        kind = _string(representation.get("kind"))
        if kind is not None:
            representations[kind] += 1
        item_diagnostics = _items(item.get("diagnostics"))
        if item_diagnostics is not None:
            diagnostics += len(item_diagnostics)
    output_bytes = sum(_integer(item.get("size")) for item in files) + sum(
        _integer(item.get("size")) for item in _objects(raw_stubs)
    )
    passthrough = representations["passthrough"]
    statistics: JsonObject = {
        "files": len(raw_files),
        "actionscript_files": len(raw_actionscript),
        "stub_files": len(raw_stubs),
        "output_bytes": output_bytes,
        "passthrough_files": passthrough,
        "transformed_files": len(files) - passthrough,
        "diagnostics": diagnostics,
    }
    return _with_rate(statistics, numerator=output_bytes, duration_seconds=duration_seconds)


def _readable_plan_statistics(payload: JsonObject) -> JsonObject:
    raw_entries = _items(payload.get("entries"))
    if raw_entries is None:
        return {}
    entries = _objects(raw_entries)
    passthrough_files = 0
    actionscript_libraries = 0
    for item in entries:
        if _string(item.get("representation")) == "passthrough":
            passthrough_files += 1
        if _string(item.get("actionscript_bundle")) is not None:
            actionscript_libraries += 1
    return {
        "files": len(entries),
        "transform_files": len(entries) - passthrough_files,
        "actionscript_libraries": actionscript_libraries,
        "passthrough_files": passthrough_files,
    }


def _readable_transform_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_files = _items(payload.get("files"))
    if raw_files is None:
        return {}
    files = _objects(raw_files)
    output_bytes = sum(_integer(item.get("size")) for item in files)
    diagnostics = sum(len(_items(item.get("diagnostics")) or []) for item in files)
    statistics: JsonObject = {
        "files": len(files),
        "output_bytes": output_bytes,
        "diagnostics": diagnostics,
    }
    return _with_rate(statistics, numerator=output_bytes, duration_seconds=duration_seconds)


def _actionscript_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_files = _items(payload.get("files"))
    if raw_files is None:
        return {}
    files = _objects(raw_files)
    output_bytes = sum(_integer(item.get("size")) for item in files)
    libraries = {_string((_object(item.get("source")) or {}).get("path")) for item in files}
    libraries.discard(None)
    statistics: JsonObject = {
        "libraries": len(libraries),
        "files": len(files),
        "output_bytes": output_bytes,
    }
    return _with_rate(statistics, numerator=output_bytes, duration_seconds=duration_seconds)


def _readable_assembly_statistics(payload: JsonObject) -> JsonObject:
    raw_files = _items(payload.get("files"))
    raw_actionscript = _items(payload.get("actionscript_files"))
    if raw_files is None or raw_actionscript is None:
        return {}
    passthrough = 0
    for item in _objects(raw_files):
        representation = _object(item.get("representation")) or {}
        if _string(representation.get("kind")) == "passthrough":
            passthrough += 1
    return {
        "files": len(raw_files),
        "actionscript_files": len(raw_actionscript),
        "passthrough_files": passthrough,
    }


def _engine_stubs_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    raw_files = _items(payload.get("files"))
    if raw_files is None:
        return {}
    files = _objects(raw_files)
    output_bytes = sum(_integer(item.get("size")) for item in files)
    typing_stubs = sum((_string(item.get("path")) or "").lower().endswith(".pyi") for item in files)
    statistics: JsonObject = {
        "files": len(files),
        "typing_stubs": typing_stubs,
        "output_bytes": output_bytes,
    }
    return _with_rate(statistics, numerator=output_bytes, duration_seconds=duration_seconds)


def _snapshot_statistics(
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    required = (
        "file_records",
        "actionscript_records",
        "stub_records",
        "package_records",
        "conflict_records",
    )
    if any(key not in payload for key in required):
        return {}
    file_records = _integer(payload.get("file_records"))
    actionscript_records = _integer(payload.get("actionscript_records"))
    stub_records = _integer(payload.get("stub_records"))
    package_records = _integer(payload.get("package_records"))
    conflict_records = _integer(payload.get("conflict_records"))
    records = (
        file_records + actionscript_records + stub_records + package_records + conflict_records
    )
    statistics: JsonObject = {
        "file_records": file_records,
        "actionscript_records": actionscript_records,
        "stub_records": stub_records,
        "package_records": package_records,
        "conflict_records": conflict_records,
        "records": records,
    }
    timings = _object(payload.get("timings"))
    if timings is not None:
        for key in (
            "populate_seconds",
            "seal_seconds",
            "verify_descriptor_seconds",
            "verify_manifests_seconds",
            "verify_payload_seconds",
            "publish_seconds",
        ):
            value = _number(timings.get(key))
            if value is not None:
                statistics[key] = round(value, 6)
    return _with_rate(
        statistics,
        numerator=records,
        duration_seconds=duration_seconds,
        key="records_per_second",
    )


def stage_statistics(
    stage: Stage,
    payload: JsonObject,
    duration_seconds: float | None,
) -> JsonObject:
    """Derive compact counters in one pass over an already validated Stage payload."""

    if stage is Stage.RESOLVE:
        return _resolve_statistics(payload)
    if stage is Stage.PLAN_ACQUISITION:
        return _plan_statistics(payload)
    if stage is Stage.DOWNLOAD:
        return _download_statistics(payload, duration_seconds)
    if stage is Stage.VERIFY:
        return _verify_statistics(payload, duration_seconds)
    if stage is Stage.ASSEMBLE_CLIENT:
        return _assemble_statistics(payload, duration_seconds)
    if stage is Stage.INDEX_VFS:
        return _index_statistics(payload)
    if stage is Stage.MATERIALIZE_VFS:
        return _materialize_statistics(payload, duration_seconds)
    if stage is Stage.PLAN_READABLE:
        return _readable_plan_statistics(payload)
    if stage is Stage.TRANSFORM_READABLE:
        return _readable_transform_statistics(payload, duration_seconds)
    if stage is Stage.DECOMPILE_ACTIONSCRIPT:
        return _actionscript_statistics(payload, duration_seconds)
    if stage is Stage.ASSEMBLE_READABLE:
        return _readable_assembly_statistics(payload)
    if stage is Stage.GENERATE_ENGINE_STUBS:
        return _engine_stubs_statistics(payload, duration_seconds)
    if stage is Stage.FINALIZE_READABLE:
        return _readable_statistics(payload, duration_seconds)
    if stage is Stage.SNAPSHOT:
        return _snapshot_statistics(payload, duration_seconds)
    return {}


__all__ = ["stage_statistics"]
