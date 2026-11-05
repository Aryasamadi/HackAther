from ares.telegram_bot import ARESBot


def test_typing_indicator_is_enabled_for_active_strix_agents() -> None:
    assert ARESBot._has_active_agent({"agents": [{"status": "running"}]})
    assert ARESBot._has_active_agent({"agents": [{"status": "AgentStatus.THINKING"}]})


def test_typing_indicator_is_disabled_when_no_agent_is_working() -> None:
    assert not ARESBot._has_active_agent({"agents": []})
    assert not ARESBot._has_active_agent({"agents": [{"status": "completed"}]})
    assert not ARESBot._has_active_agent({"agents": [{"status": "idle"}]})
    assert not ARESBot._has_active_agent({"agents": "invalid"})
