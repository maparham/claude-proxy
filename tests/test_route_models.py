"""Route model metadata (spec 3): what /v1/models lists for third-party routes, and the sizes OpenCode needs."""
import pytest
from fastapi.responses import JSONResponse

from claude_proxy.app import create_app, route_model_entries
from claude_proxy.config import Config, ConfigError, Route
from tests.conftest import asgi_client
from tests.test_proxy import setup  # noqa: F401  (fixture)

MUSE = {"type": "model", "id": "muse-spark", "display_name": "Muse Spark 1.3", "created_at": "2026-01-01T00:00:00Z",
        "max_input_tokens": 1048576, "max_tokens": 32000}


def route(**kw):
    return Route(name="x", base_url="http://x", api_key_env="X", models=["m*"], **kw)


def test_listed_models_are_the_map_and_info_keys_in_order():
    r = route(model_map={"a": "a-1"}, model_info={"b": {"context": 10, "output": 5}, "a": {"display_name": "A"}})
    assert r.listed_models() == ["a", "b"]


def test_default_muse_entry_carries_its_sizes():
    assert route_model_entries(Config()) == [MUSE]


def test_entry_without_sizes_has_no_size_fields():
    cfg = Config()
    cfg.routes[0].model_info = {}
    assert route_model_entries(cfg) == [{"type": "model", "id": "muse-spark", "display_name": "muse-spark (via meta)",
                                         "created_at": "2026-01-01T00:00:00Z"}]


def test_a_glob_only_route_lists_nothing():
    cfg = Config()
    cfg.routes.append(Route(name="other", base_url="http://o", api_key_env="O", models=["foo-*"]))
    assert [m["id"] for m in route_model_entries(cfg)] == ["muse-spark"]


@pytest.mark.parametrize("info,error", [
    ({"m1": {"context": 1000}}, "both context and output"),
    ({"m1": {"context": 1000, "output": 0}}, "positive whole number"),
    ({"m1": {"context": "1M", "output": 10}}, "positive whole number"),
    ({"m1": {"context": True, "output": 10}}, "positive whole number"),
    ({"m1": {"ctx": 1}}, "unknown keys"),
])
def test_bad_model_info_is_refused(info, error):
    with pytest.raises(TypeError, match=error):
        route(model_info=info)


def test_bad_model_info_in_a_config_file_is_a_config_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[[routes]]\nname = "meta"\nbase_url = "http://x"\napi_key_env = "X"\nmodels = ["m*"]\n'
                 'model_info = { m1 = { context = 1000 } }\n')
    with pytest.raises(ConfigError, match="both context and output"):
        Config.load(str(p))


async def test_full_key_model_list_appends_route_models_with_sizes(setup, anthropic):  # noqa: F811
    gw, conn, uid, h = setup
    anthropic.default = lambda req: JSONResponse({"data": [{"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"}],
                                                  "has_more": False, "first_id": "claude-sonnet-5", "last_id": "claude-sonnet-5"})
    async with asgi_client(create_app(gw)) as c:
        r = await c.get("/v1/models", headers=h)
    assert r.json()["data"][1] == MUSE
