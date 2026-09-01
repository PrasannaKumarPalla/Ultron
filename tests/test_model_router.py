from ultron.model_router import route_general_chat_model, route_model


def test_general_chat_prefers_responsive_model():
    model, reason = route_general_chat_model({"qwen3:30b", "phi4:latest"}, "qwen3:30b")
    assert model == "phi4:latest"
    assert "responsive" in reason


def test_routes_coding_project_to_coder():
    model, reason = route_model("Portal", "Web app", "Implement and test the backend API",
                                {"qwen3-coder:30b", "qwen3.6:27b"}, "qwen3:30b")
    assert model == "qwen3-coder:30b"
    assert "coding and implementation" in reason


def test_routes_architecture_project_to_reasoner():
    model, reason = route_model("Cloud AI", "Architecture", "Design a secure agent and RAG strategy",
                                {"qwen3-coder:30b", "qwen3.6:27b"}, "qwen3:30b")
    assert model == "qwen3.6:27b"
    assert "architecture and reasoning" in reason


def test_router_uses_installed_fallback():
    model, _ = route_model("Unknown", "", "Do something unusual", {"qwen3:30b"}, "qwen3:30b")
    assert model == "qwen3:30b"
