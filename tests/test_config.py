from incidentlens_control_plane.config import RuntimeSettings


def test_default_run_budget_can_reach_the_provider_decision_cadence(tmp_path) -> None:
    settings = RuntimeSettings(data_dir=tmp_path)

    assert settings.max_rounds_per_run > 12
    assert settings.max_tool_calls_per_run >= settings.max_rounds_per_run * 2


def test_semantic_compact_threshold_fraction_is_below_max_input(tmp_path) -> None:
    settings = RuntimeSettings(data_dir=tmp_path)
    assert 0 < settings.agent_context_semantic_compact_at_fraction <= 1
    # The pressure threshold derives from max_input_tokens below the hard ceiling.
    max_input = (
        settings.agent_context_window_tokens
        - settings.agent_context_max_output_tokens
        - settings.agent_context_reserve_tokens
    )
    assert (
        settings.agent_context_semantic_compact_at_fraction * max_input < max_input
    )


def test_context_budget_environment_overrides_are_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INCIDENTLENS_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_WINDOW_TOKENS", "8000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_MAX_OUTPUT_TOKENS", "1000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_RESERVE_TOKENS", "1000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_TOOL_RESULT_BUDGET_CHARS", "10000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_MICRO_COMPACT_AFTER_SECONDS", "7200")
    monkeypatch.setenv(
        "INCIDENTLENS_AGENT_CONTEXT_SEMANTIC_COMPACT_AT_FRACTION", "0.85"
    )
    monkeypatch.setenv("INCIDENTLENS_MAX_ROUNDS_PER_RUN", "24")
    monkeypatch.setenv("INCIDENTLENS_MAX_TOOL_CALLS_PER_RUN", "64")
    monkeypatch.setenv("INCIDENTLENS_MAX_NO_NEW_EVIDENCE_ROUNDS", "12")
    monkeypatch.setenv("INCIDENTLENS_MAX_PROVIDER_RETRIES", "5")
    monkeypatch.setenv("INCIDENTLENS_MAX_INVESTIGATION_ROUNDS", "96")
    monkeypatch.setenv("INCIDENTLENS_MAX_INVESTIGATION_TOOL_CALLS", "192")

    settings = RuntimeSettings.from_environment()

    assert settings.agent_context_window_tokens == 8000
    assert settings.agent_context_max_output_tokens == 1000
    assert settings.agent_context_reserve_tokens == 1000
    assert settings.agent_tool_result_budget_chars == 10000
    assert settings.agent_micro_compact_after_seconds == 7200
    assert settings.agent_context_semantic_compact_at_fraction == 0.85
    assert settings.max_rounds_per_run == 24
    assert settings.max_tool_calls_per_run == 64
    assert settings.max_no_new_evidence_rounds == 12
    assert settings.max_provider_retries == 5
    assert settings.max_investigation_rounds == 96
    assert settings.max_investigation_tool_calls == 192
