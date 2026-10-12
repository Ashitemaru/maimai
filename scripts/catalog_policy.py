"""Machine-readable source boundaries shared by catalog builds and consumers."""

from copy import deepcopy

SOURCE_POLICY = {
    "policy_version": 5,
    "entity": {
        "schema_version": 2,
        "primary_key": ["id", "difficulty"],
        "layout": "flat_charts",
        "dx_id_offset": 10000,
        "admission": "exclude_entire_chart_until_all_conflicting_fields_adjudicated",
    },
    "llm_used": False,
    "cn_catalog": {
        "authority": "lxns",
        "fields": ["songs", "charts", "current_constants", "notes", "aliases", "cn_versions"],
    },
    "manual_field_exceptions": {
        "resolution": "explicit_user_source_choice",
        "automatic_sources": ["lxns", "divingfish"],
        "shared_metadata": ["title", "artist", "bpm", "genre"],
        "cn_chart": ["level", "constant", "note_designer", "notes"],
        "forbidden_cross_region_fields": ["level", "constant", "cn_version", "jp_version"],
        "decision_lifetime": "permanent_until_explicit_revoke",
        "source_changes_invalidate_decision": False,
        "all_conflicting_fields_required": True,
        "raw_identity_for_jp_matching": True,
    },
    "waterfish": {
        "source": "divingfish",
        "roles": ["cross_validation", "external_ids"],
        "jp_release_authority": False,
    },
    "jp_ordinary_release": {
        "authority": "arcade_songs",
        "field": "songs[].sheets[].version",
        "only_imported_field": "jp_release_version",
        "metadata_comparison": False,
        "chart_types": ["standard", "dx"],
        "difficulty_indices": [0, 1, 2, 3],
        "match": ["normalized_title", "chart_type"],
        "same_title_disambiguation": "normalized_artist_or_unique_source_and_local_record",
        "forbidden_fallbacks": [
            "song.version",
            "regionOverrides",
            "cn_version",
            "sega_song_version",
        ],
    },
    "jp_remaster_release": {
        "authority": "remywiki",
        "chart_types": ["standard", "dx"],
        "difficulty_index": 4,
        "missing_evidence": "pending",
        "blocks_catalog_update": False,
    },
    "conflict_policy": {
        "automatic_overwrite": False,
        "resolution": "whole_chart_user_adjudication",
        "definition": "more_than_one_distinct_non_missing_comparable_value",
        "status_flags_are_conflicts": False,
        "single_value": "adopt_automatically",
        "historical_snapshot_is_source": False,
        "choices_bound_at_submission": True,
        "chosen_values_durable": True,
        "pending_in_public_catalog": False,
    },
    "sega": {"automated_fetch": False, "jp_release_authority": False},
}


def source_policy():
    return deepcopy(SOURCE_POLICY)
