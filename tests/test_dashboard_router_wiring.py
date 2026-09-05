"""Routes extracted out of `main.py` must stay mounted and stay reachable.

`main.py` is being decomposed into `brain/dashboard/api/routers/`. The failure
mode that decomposition invites is silent: a route moves out of `main.py`, the
`include_router` call is forgotten or the import is dropped, every unit test
still passes because they call the handler functions directly, and the endpoint
simply 404s in production.

These tests exercise the mounted application rather than the handlers, so a
route that is defined but not wired fails here.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from brain.dashboard.api.main import app

# A loopback peer is trusted without a token, so these reach routing rather
# than stopping at the authorization middleware.
LOOPBACK = ("127.0.0.1", 50000)

# Endpoints owned by an extracted router. Each entry is (method, path).
# `runtime.job_queue` is None outside `lifespan`, so a *mounted* route answers
# 503 "Server not initialized" from `runtime.require_job`. An unmounted one
# answers 404. That difference is the whole test.
EXTRACTED_ROUTES = [
    # routers/pronunciations.py
    ("GET", "/api/projects/pinned/pronunciations"),
    ("POST", "/api/projects/pinned/pronunciations"),
    ("POST", "/api/projects/pinned/pronunciations/batch"),
    ("POST", "/api/projects/pinned/pronunciations/preview"),
    ("GET", "/api/projects/pinned/pronunciations/preview/abc/audio"),
    # routers/voice_cast.py
    ("GET", "/api/projects/pinned/characters"),
    ("GET", "/api/projects/pinned/voices"),
    ("GET", "/api/projects/pinned/voices/download-all"),
    ("GET", "/api/projects/pinned/voices/v1/preview"),
    ("GET", "/api/projects/pinned/voices/v1/download"),
    ("PATCH", "/api/projects/pinned/characters/c1/voice"),
    ("PATCH", "/api/projects/pinned/characters/c1/profile"),
    ("POST", "/api/projects/pinned/voices/v1/regenerate"),
    ("POST", "/api/projects/pinned/voices/v1/upload"),
    # routers/external_validation.py
    ("GET", "/api/projects/pinned/external-validation/events"),
    ("GET", "/api/projects/pinned/external-validation/status"),
    ("POST", "/api/projects/pinned/external-validation/retry"),
    # routers/quality.py -- moved once runtime owned the pipeline-start
    # indirection, which is what had kept this group in main.
    ("GET", "/api/projects/pinned/quality"),
    ("GET", "/api/projects/pinned/quality/review"),
    ("POST", "/api/projects/pinned/quality/review"),
    ("GET", "/api/projects/pinned/reviews"),
    # still in main.py: approve_voice_cast calls start_pipeline directly rather
    # than through runtime, and moving it buys little. Pinned so that stays a
    # decision rather than an oversight.
    ("POST", "/api/projects/pinned/voice-review/approve"),
]


class RouterWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, client=LOOPBACK)

    def test_extracted_routes_are_mounted(self) -> None:
        # An *unmounted* route is Starlette's routing 404, whose body is exactly
        # {"detail": "Not Found"}. A mounted handler may legitimately answer 404
        # too (`{"detail": "Characters not analyzed yet"}`), so the status alone
        # cannot tell the two apart and the body is what discriminates.
        for method, path in EXTRACTED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                response = self.client.request(method, path, json={})
                if response.status_code != 404:
                    continue
                detail = ""
                try:
                    detail = response.json().get("detail", "")
                except ValueError:
                    pass
                self.assertNotEqual(
                    detail,
                    "Not Found",
                    f"{method} {path} is not mounted; check include_router in main.py",
                )

    #: route -> the function that must serve it. Mountedness is not enough:
    #: a route bound to the wrong handler is still mounted, which is how
    #: `GET /characters` came to approve the voice cast.
    ROUTE_OWNERS = {
        ("get", "/api/projects/{project_id}/characters"): "get_characters",
        ("get", "/api/projects/{project_id}/voices"): "get_project_voices",
        ("post", "/api/projects/{project_id}/voice-review/approve"): "approve_voice_cast",
        ("get", "/api/projects/{project_id}/external-validation/events"): "get_external_validation_events",
        ("get", "/api/projects/{project_id}/external-validation/status"): "get_external_validation_status",
        ("post", "/api/projects/{project_id}/external-validation/retry"): "retry_external_validation",
        ("get", "/api/projects/{project_id}/quality"): "get_quality_report",
        ("get", "/api/projects/{project_id}/quality/review"): "get_quality_review",
        ("post", "/api/projects/{project_id}/quality/review"): "update_quality_review",
        ("get", "/api/projects/{project_id}/pronunciations"): "get_pronunciations",
        ("post", "/api/system/restart"): "restart_dashboard_server",
        ("post", "/api/system/shutdown"): "shutdown_dashboard",
        ("post", "/api/system/release-gpu"): "release_gpu",
    }

    def test_each_route_is_served_by_its_own_handler(self) -> None:
        """Extracting a router must not leave a decorator behind.

        The extraction located functions by AST `lineno`, which points at
        `def` and not at the decorators above it. Bodies moved into the
        routers; the `@app.get`/`@app.post` lines stayed in main.py and bound
        themselves to whichever function was defined next. Four routes ended up
        calling the wrong handler:

            GET  /characters                  -> approve_voice_cast
            GET  /external-validation/status  -> restart_dashboard_server
            POST /external-validation/retry   -> restart_dashboard_server
            GET  /external-validation/events  -> get_quality_report

        Two of those restarted the dashboard. The review inbox polls
        `/external-validation/status`, so every poll killed the running
        pipeline -- which is what the unexplained restarts of 2026-09-04 were.

        `test_extracted_routes_are_mounted` could not catch it: a
        wrongly-bound route is still mounted.
        """
        spec = app.openapi()
        wrong = []
        for (method, path), owner in self.ROUTE_OWNERS.items():
            operation = spec["paths"].get(path, {}).get(method)
            if operation is None:
                wrong.append(f"{method.upper()} {path} is not registered at all")
                continue
            # FastAPI builds operationId as "<func>_<mangled path>_<method>".
            actual = operation.get("operationId", "").split("_api_")[0]
            if actual != owner:
                wrong.append(f"{method.upper()} {path} -> {actual}, expected {owner}")
        self.assertEqual(wrong, [], "routes bound to the wrong handler:\n  " + "\n  ".join(wrong))

    def test_no_handler_carries_routes_from_two_different_features(self) -> None:
        """Catch the shape of the bug, not just the four known instances.

        A function legitimately serves two paths (`/health` and `/api/health`).
        It does not legitimately serve paths from unrelated features, which is
        what an orphaned decorator produces.
        """
        spec = app.openapi()
        by_handler: dict[str, list[str]] = {}
        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                handler = operation.get("operationId", "").split("_api_")[0]
                by_handler.setdefault(handler, []).append(f"{method.upper()} {path}")

        def feature(route: str) -> str:
            path = route.split(" ", 1)[1]
            tail = path.replace("/api/projects/{project_id}", "").replace("/api", "")
            return tail.strip("/").split("/")[0] or "root"

        suspicious = []
        for handler, routes in by_handler.items():
            features = {feature(r) for r in routes}
            # health/api-health differ only by prefix and collapse to one feature.
            if len(features) > 1:
                suspicious.append(f"{handler} serves {sorted(features)}: {routes}")
        self.assertEqual(
            suspicious,
            [],
            "one handler is serving routes from unrelated features, which means a "
            "decorator was left behind by an extraction:\n  " + "\n  ".join(suspicious),
        )

    def test_an_unmounted_path_really_does_404(self) -> None:
        """Guard the guard: the discriminator above must actually fire."""
        response = self.client.get("/api/projects/pinned/definitely-not-a-route")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json().get("detail"), "Not Found")

    def test_the_pipeline_starter_is_registered(self) -> None:
        """The inversion is only real if main actually fills the slot.

        `runtime.schedule_resume_after_reviews` resumes a paused run through
        `runtime.start_pipeline`. If main never registers its implementation
        that raises 503, and a project whose last blocking review was just
        resolved would silently never restart -- a failure with no error
        anywhere, only a run that does not happen.
        """
        from brain.dashboard.api import main, runtime

        self.assertIsNotNone(
            runtime._pipeline_starter,
            "main must call runtime.register_pipeline_starter at import",
        )
        self.assertIs(runtime._pipeline_starter, main.start_pipeline)

    def test_routers_do_not_import_main(self) -> None:
        """The dependency must run one way, or the split buys nothing.

        A router importing `main` would recreate the cycle the extraction is
        meant to remove, and would reintroduce import-time coupling to the
        whole 5,000-line module.
        """
        import pkgutil
        from pathlib import Path

        import brain.dashboard.api.routers as routers_pkg

        offenders = []
        for module in pkgutil.iter_modules(routers_pkg.__path__):
            source = (Path(routers_pkg.__path__[0]) / f"{module.name}.py").read_text(encoding="utf-8")
            if "api.main" in source or "from .main" in source:
                offenders.append(module.name)
        self.assertEqual(offenders, [], "routers must not import main")


if __name__ == "__main__":
    unittest.main()
