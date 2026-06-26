"""Unit tests for config loading and the FastMCP lifespan wiring (U2).

Credential-free throughout: ``Config.from_env()`` is driven via monkeypatched
environment variables, and ``app_lifespan`` is driven with a monkeypatched
``GCSClient`` (the module-global the lifespan constructs) backed by the shared
in-memory :class:`FakeGCSClient`, so the startup-reachability fail-fast path is
exercised without a live bucket or any credentials.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from static_hosting_mcp import server
from static_hosting_mcp.config import (
    ENV_BUCKET,
    ENV_CREDENTIALS,
    ENV_PROJECT,
    Config,
)
from static_hosting_mcp.gcs_client import GCSClientProtocol

from .fakes import FakeGCSClient

_ALL_ENV = (ENV_BUCKET, ENV_CREDENTIALS, ENV_PROJECT)
_ABS_KEY = "/etc/secrets/static-hosting-sa.json"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every config env var so each test sets exactly what it needs."""
    for var in _ALL_ENV:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Config.from_env
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
class TestConfigFromEnv:
    def test_happy_path_all_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, _ABS_KEY)
        monkeypatch.setenv(ENV_PROJECT, "my-project")

        config = Config.from_env()

        assert config.bucket == "artifacts-bucket"
        assert config.key_path == _ABS_KEY
        assert config.project == "my-project"

    def test_missing_bucket_only_names_only_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CREDENTIALS, _ABS_KEY)

        with pytest.raises(ValueError) as excinfo:
            Config.from_env()

        msg = str(excinfo.value)
        assert ENV_BUCKET in msg
        # Only the genuinely-missing var is named — the one that is set is not.
        assert ENV_CREDENTIALS not in msg

    def test_missing_both_required_names_both(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Config.from_env()

        msg = str(excinfo.value)
        assert ENV_BUCKET in msg
        assert ENV_CREDENTIALS in msg

    def test_project_optional_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, _ABS_KEY)

        config = Config.from_env()

        assert config.project is None

    def test_empty_project_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, _ABS_KEY)
        monkeypatch.setenv(ENV_PROJECT, "")

        assert Config.from_env().project is None

    def test_relative_credentials_rejected_actionably(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, "relative/sa.json")

        with pytest.raises(ValueError) as excinfo:
            Config.from_env()

        msg = str(excinfo.value)
        assert ENV_CREDENTIALS in msg
        assert "absolute" in msg.lower()

    def test_missing_check_precedes_absolute_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bucket missing AND a relative key path: the fail-fast missing-var
        # aggregation wins so the operator fixes the unset var first.
        monkeypatch.setenv(ENV_CREDENTIALS, "relative/sa.json")

        with pytest.raises(ValueError) as excinfo:
            Config.from_env()

        assert "Missing required environment variables" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Config secrecy / immutability
# ---------------------------------------------------------------------------


class TestConfigSecrecy:
    def test_key_path_absent_from_repr_and_str(self) -> None:
        config = Config(bucket="visible-bucket", key_path="/keys/super-secret-sa.json")

        for rendered in (repr(config), str(config)):
            assert "super-secret-sa.json" not in rendered
            assert "/keys/" not in rendered
        # The bucket is not a secret and legitimately appears.
        assert "visible-bucket" in repr(config)

    def test_config_is_frozen(self) -> None:
        config = Config(bucket="b", key_path=_ABS_KEY)

        with pytest.raises(FrozenInstanceError):
            config.bucket = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# app_lifespan
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
class TestAppLifespan:
    @pytest.mark.asyncio
    async def test_yields_appcontext_with_client_and_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, _ABS_KEY)
        fake = FakeGCSClient("artifacts-bucket")
        monkeypatch.setattr(server, "GCSClient", lambda *a, **k: fake)

        async with server.app_lifespan(server.mcp) as ctx:
            assert isinstance(ctx, server.AppContext)
            assert ctx.client is fake
            assert ctx.config.bucket == "artifacts-bucket"
            assert ctx.config.key_path == _ABS_KEY

    @pytest.mark.asyncio
    async def test_constructs_client_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The lifespan must build the client from the key path + optional
        # project (never invented), so credentials stay env-sourced.
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, _ABS_KEY)
        monkeypatch.setenv(ENV_PROJECT, "proj-42")
        seen: dict[str, Any] = {}

        def _factory(bucket: str, **kwargs: Any) -> FakeGCSClient:
            seen["bucket"] = bucket
            seen["kwargs"] = kwargs
            return FakeGCSClient(bucket)

        monkeypatch.setattr(server, "GCSClient", _factory)

        async with server.app_lifespan(server.mcp):
            pass

        assert seen["bucket"] == "artifacts-bucket"
        assert seen["kwargs"]["key_path"] == _ABS_KEY
        assert seen["kwargs"]["project"] == "proj-42"

    @pytest.mark.asyncio
    async def test_startup_error_exits_clean_and_names_bucket(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, _ABS_KEY)
        # reachable=False makes the fake's check_reachable raise StartupError.
        fake = FakeGCSClient("artifacts-bucket", reachable=False)
        monkeypatch.setattr(server, "GCSClient", lambda *a, **k: fake)

        with pytest.raises(SystemExit) as excinfo:
            async with server.app_lifespan(server.mcp):
                pytest.fail("lifespan must not yield when the bucket is unreachable")

        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        # A clean, actionable line that names the bucket — not a raw traceback.
        assert "artifacts-bucket" in err
        assert "Traceback" not in err

    @pytest.mark.asyncio
    async def test_missing_env_exits_clean_before_client_built(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # RF6: a missing-var ValueError from from_env() is converted to the same
        # clean stderr + exit(1) contract as the reachability fail-fast (not a raw
        # traceback), and still happens before any client is constructed.
        built = {"called": False}

        def _factory(*_a: Any, **_k: Any) -> FakeGCSClient:
            built["called"] = True
            return FakeGCSClient("x")

        monkeypatch.setattr(server, "GCSClient", _factory)

        with pytest.raises(SystemExit) as excinfo:
            async with server.app_lifespan(server.mcp):
                pass

        assert excinfo.value.code == 1
        assert built["called"] is False
        err = capsys.readouterr().err
        assert "Missing required environment variables" in err
        assert "Traceback" not in err

    @pytest.mark.asyncio
    async def test_relative_credentials_exit_clean_before_client_built(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # RF6: a *relative* GOOGLE_APPLICATION_CREDENTIALS passes a presence-only
        # check but fails from_env()'s absolute-path validation. The lifespan must
        # turn that ValueError into the clean exit contract for every entry point
        # (mcp dev / FastMCP CLI), not a raw anyio traceback, and never build the
        # client.
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, "relative/sa.json")
        built = {"called": False}

        def _factory(*_a: Any, **_k: Any) -> FakeGCSClient:
            built["called"] = True
            return FakeGCSClient("x")

        monkeypatch.setattr(server, "GCSClient", _factory)

        with pytest.raises(SystemExit) as excinfo:
            async with server.app_lifespan(server.mcp):
                pass

        assert excinfo.value.code == 1
        assert built["called"] is False
        err = capsys.readouterr().err
        assert "absolute" in err.lower()
        assert "Traceback" not in err


# ---------------------------------------------------------------------------
# _close_client (best-effort teardown seam)
# ---------------------------------------------------------------------------


class TestCloseClient:
    @pytest.mark.asyncio
    async def test_calls_sync_close_when_present(self) -> None:
        class Closeable:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        c = Closeable()
        await server._close_client(cast(GCSClientProtocol, c))
        assert c.closed is True

    @pytest.mark.asyncio
    async def test_awaits_async_aclose_when_present(self) -> None:
        class AsyncCloseable:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        c = AsyncCloseable()
        await server._close_client(cast(GCSClientProtocol, c))
        assert c.closed is True

    @pytest.mark.asyncio
    async def test_noop_when_client_exposes_no_teardown(self) -> None:
        # The shipped GCSClient / FakeGCSClient expose neither close nor aclose;
        # teardown must be a clean no-op rather than an AttributeError.
        fake = FakeGCSClient("b")
        assert not hasattr(fake, "close")
        assert not hasattr(fake, "aclose")
        await server._close_client(fake)  # must not raise


# ---------------------------------------------------------------------------
# main() entry-point fail-fast (RF6 / testing T4)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
class TestMainEntryPoint:
    """RF6: ``main()`` delegates validation to ``Config.from_env()`` so its
    fail-fast inherits the absolute-path check the old presence-only loop skipped.
    Each case raises before ``mcp.run()`` is reached, so the transport never starts.
    """

    def test_main_missing_env_exits_naming_missing_vars(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from static_hosting_mcp import main

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "Missing required environment variables" in err
        assert ENV_BUCKET in err
        assert ENV_CREDENTIALS in err

    def test_main_relative_credentials_exits_with_absolute_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gap RF6 closes: a relative key path passed main()'s old presence-only
        # check and only blew up later as a raw lifespan traceback.
        monkeypatch.setenv(ENV_BUCKET, "artifacts-bucket")
        monkeypatch.setenv(ENV_CREDENTIALS, "relative/sa.json")
        from static_hosting_mcp import main

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        assert "absolute" in capsys.readouterr().err.lower()
