from incidentlens_control_plane.config import RuntimeSettings


def test_context_budget_environment_overrides_are_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INCIDENTLENS_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_WINDOW_TOKENS", "8000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_MAX_OUTPUT_TOKENS", "1000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_RESERVE_TOKENS", "1000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_TOOL_RESULT_BUDGET_CHARS", "10000")
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_MAX_MESSAGE_GROUPS", "10")
    monkeypatch.setenv("INCIDENTLENS_AGENT_CONTEXT_KEEP_RECENT_TOOL_RESULTS", "1")
    monkeypatch.setenv("INCIDENTLENS_MAX_ROUNDS_PER_RUN", "24")
    monkeypatch.setenv("INCIDENTLENS_MAX_TOOL_CALLS_PER_RUN", "64")
    monkeypatch.setenv("INCIDENTLENS_MAX_NO_NEW_EVIDENCE_ROUNDS", "12")
    monkeypatch.setenv("INCIDENTLENS_MAX_PROVIDER_RETRIES", "5")

    settings = RuntimeSettings.from_environment()

    assert settings.agent_context_window_tokens == 8000
    assert settings.agent_context_max_output_tokens == 1000
    assert settings.agent_context_reserve_tokens == 1000
    assert settings.agent_tool_result_budget_chars == 10000
    assert settings.agent_context_max_message_groups == 10
    assert settings.agent_context_keep_recent_tool_results == 1
    assert settings.max_rounds_per_run == 24
    assert settings.max_tool_calls_per_run == 64
    assert settings.max_no_new_evidence_rounds == 12
    assert settings.max_provider_retries == 5
