import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from brain.dashboard.api.security import (
    DEFAULT_TRUSTED_LAN_CIDRS,
    configured_dashboard_token,
    configured_trusted_lan_cidrs,
    dashboard_request_authorized,
    is_cross_site_mutation,
    is_loopback_client,
    is_private_client,
)
from shared.config_validation import validate_brain_config


class DashboardSecurityTests(unittest.TestCase):
    def test_cross_site_browser_mutations_are_rejected(self):
        self.assertTrue(is_cross_site_mutation("POST", "cross-site"))
        self.assertFalse(is_cross_site_mutation("GET", "cross-site"))
        self.assertFalse(is_cross_site_mutation("POST", "same-origin"))
        self.assertFalse(is_cross_site_mutation("POST", None))

    def test_loopback_detection_supports_ipv4_ipv6_and_mapped_addresses(self):
        self.assertTrue(is_loopback_client("127.0.0.1"))
        self.assertTrue(is_loopback_client("::1"))
        self.assertTrue(is_loopback_client("::ffff:127.0.0.1"))
        self.assertTrue(is_loopback_client("localhost"))
        self.assertFalse(is_loopback_client("192.0.2.10"))
        self.assertFalse(is_loopback_client(None))

    def test_remote_dashboard_requests_fail_closed_without_a_token(self):
        self.assertFalse(
            dashboard_request_authorized(
                client_host="192.0.2.10",
                configured_token="",
                presented_token=None,
            )
        )

    def test_remote_dashboard_requests_require_the_exact_token(self):
        self.assertTrue(
            dashboard_request_authorized(
                client_host="192.0.2.10",
                configured_token="correct",
                presented_token="correct",
            )
        )
        self.assertFalse(
            dashboard_request_authorized(
                client_host="192.0.2.10",
                configured_token="correct",
                presented_token="wrong",
            )
        )

    def test_private_lan_requests_do_not_need_a_token(self):
        self.assertTrue(is_private_client("192.168.50.194"))
        self.assertTrue(
            dashboard_request_authorized(
                client_host="192.168.50.194",
                configured_token="",
                presented_token=None,
            )
        )

    def test_tailscale_cgnat_requests_do_not_need_a_token(self):
        self.assertTrue(is_private_client("100.106.68.80"))
        self.assertTrue(
            dashboard_request_authorized(
                client_host="100.106.68.80",
                configured_token="",
                presented_token=None,
            )
        )

    def test_reserved_documentation_range_is_not_trusted_lan(self):
        self.assertFalse(is_private_client("192.0.2.10"))

    def test_loopback_dashboard_requests_do_not_need_the_remote_token(self):
        self.assertTrue(
            dashboard_request_authorized(
                client_host="127.0.0.1",
                configured_token="configured",
                presented_token=None,
            )
        )

    def test_environment_token_overrides_tracked_config(self):
        with patch.dict(
            os.environ,
            {"CRAZY_AUDIOBOOK_DASHBOARD_TOKEN": "from-environment"},
            clear=False,
        ):
            self.assertEqual(
                configured_dashboard_token({"api_token": "from-yaml"}),
                "from-environment",
            )


class ConfiguredLanBoundaryTests(unittest.TestCase):
    """`dashboard.trusted_lan_cidrs` must actually narrow the trust boundary.

    This setting is documented in the README, architecture, API reference,
    configuration and setup guides as *the* auditable control for token-free
    LAN access. It was previously never read by any code path, so the effective
    boundary was always the wide default. These tests pin the wiring.
    """

    def test_absent_key_falls_back_to_the_wide_default(self):
        self.assertEqual(
            configured_trusted_lan_cidrs({}),
            DEFAULT_TRUSTED_LAN_CIDRS,
        )

    def test_configured_list_replaces_the_default(self):
        self.assertEqual(
            configured_trusted_lan_cidrs(
                {"trusted_lan_cidrs": ["192.168.50.0/24"]}
            ),
            ("192.168.50.0/24",),
        )

    def test_a_peer_outside_the_configured_range_is_refused(self):
        """The regression that matters: inside the default, outside the config."""
        narrowed = configured_trusted_lan_cidrs(
            {"trusted_lan_cidrs": ["192.168.50.0/24"]}
        )
        # 10.1.2.3 is inside DEFAULT_TRUSTED_LAN_CIDRS but not the narrowed set.
        self.assertTrue(is_private_client("10.1.2.3", DEFAULT_TRUSTED_LAN_CIDRS))
        self.assertFalse(
            dashboard_request_authorized(
                client_host="10.1.2.3",
                configured_token="",
                presented_token=None,
                trusted_lan_cidrs=narrowed,
            )
        )

    def test_a_peer_inside_the_configured_range_is_allowed(self):
        self.assertTrue(
            dashboard_request_authorized(
                client_host="192.168.50.44",
                configured_token="",
                presented_token=None,
                trusted_lan_cidrs=("192.168.50.0/24",),
            )
        )

    def test_empty_list_disables_token_free_lan_access(self):
        self.assertEqual(
            configured_trusted_lan_cidrs({"trusted_lan_cidrs": []}),
            (),
        )
        self.assertFalse(
            dashboard_request_authorized(
                client_host="192.168.50.44",
                configured_token="",
                presented_token=None,
                trusted_lan_cidrs=(),
            )
        )
        # Loopback must still work with an empty list.
        self.assertTrue(
            dashboard_request_authorized(
                client_host="127.0.0.1",
                configured_token="",
                presented_token=None,
                trusted_lan_cidrs=(),
            )
        )

    def test_token_still_authorizes_a_peer_outside_the_range(self):
        self.assertTrue(
            dashboard_request_authorized(
                client_host="203.0.113.7",
                configured_token="secret-token",
                presented_token="secret-token",
                trusted_lan_cidrs=("192.168.50.0/24",),
            )
        )

    def test_a_single_string_is_accepted_as_one_cidr(self):
        self.assertEqual(
            configured_trusted_lan_cidrs(
                {"trusted_lan_cidrs": "192.168.50.0/24"}
            ),
            ("192.168.50.0/24",),
        )

    def test_a_malformed_type_is_rejected_rather_than_widening_trust(self):
        with self.assertRaises(ValueError):
            configured_trusted_lan_cidrs({"trusted_lan_cidrs": 42})

    def test_shipped_config_narrows_the_boundary(self):
        """The checked-in configuration must not rely on the wide default."""
        config = yaml.safe_load(
            Path("brain/config.yaml").read_text(encoding="utf-8")
        )
        dashboard = config.get("dashboard", {})
        self.assertIn(
            "trusted_lan_cidrs",
            dashboard,
            "brain/config.yaml must declare dashboard.trusted_lan_cidrs",
        )
        self.assertNotEqual(
            configured_trusted_lan_cidrs(dashboard),
            DEFAULT_TRUSTED_LAN_CIDRS,
        )

    def test_shipped_config_does_not_use_wildcard_cors(self):
        """`*` lets any page a LAN user visits read this dashboard's data."""
        config = yaml.safe_load(
            Path("brain/config.yaml").read_text(encoding="utf-8")
        )
        self.assertNotIn("*", config.get("dashboard", {}).get("cors_origins", []))


class InvalidCidrConfigurationTests(unittest.TestCase):
    def test_an_invalid_network_fails_startup_validation(self):
        with self.assertRaises(ValueError) as caught:
            validate_brain_config(
                {"dashboard": {"trusted_lan_cidrs": ["192.168.50.0/33"]}}
            )
        self.assertIn("trusted_lan_cidrs", str(caught.exception))

    def test_a_valid_network_passes_startup_validation(self):
        validate_brain_config(
            {"dashboard": {"trusted_lan_cidrs": ["192.168.50.0/24"]}}
        )


if __name__ == "__main__":
    unittest.main()
