"""镜像整数版本分配契约；只使用标准库，所有外部调用均隔离。"""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError


SCRIPT = Path(__file__).with_name("allocate_image_version.py")
SPEC = importlib.util.spec_from_file_location("allocate_image_version_under_test", SCRIPT)
allocator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(allocator)


def refs(*versions, image_name="tianshu-backend"):
    namespace = allocator.TAG_PREFIX + image_name + "/" if image_name else allocator.TAG_PREFIX
    return [{"ref": namespace + str(version)} for version in versions]


class ReservedVersionsTests(unittest.TestCase):
    def test_only_canonical_integer_reservations_at_least_three_are_counted(self):
        irrelevant = [
            "refs/tags/v3",
            "refs/tags/3",
            "refs/tags/20261120",
            "refs/tags/image-build-version/",
            "refs/tags/image-build-version/2",
            "refs/tags/image-build-version/2.1",
            "refs/tags/image-build-version/003",
            "refs/tags/image-build-version/3/child",
            "refs/tags/image-build-version/+4",
            "refs/tags/image-build-version/４",
            "refs/tags/image-build-version/ 5",
            "refs/tags/image-build-version/6\n",
            "refs/tags/image-build-version/-7",
            "refs/heads/image-build-version/8",
        ]
        self.assertEqual(
            allocator.parse_reserved_versions(
                refs(3, 10, 10) + [{"ref": value} for value in irrelevant], "tianshu-backend"
            ),
            {3, 10},
        )

    def test_each_image_uses_only_its_own_reservations(self):
        reservations = refs(3, 10) + refs(4, image_name="tianshu-frontend")
        self.assertEqual(allocator.parse_reserved_versions(reservations, "tianshu-backend"), {3, 10})
        self.assertEqual(allocator.parse_reserved_versions(reservations, "tianshu-frontend"), {4})

    def test_legacy_shared_reservations_remain_a_baseline_for_both_images(self):
        reservations = refs(3, 8, image_name=None) + refs(10) + refs(9, image_name="tianshu-frontend")
        self.assertEqual(allocator.parse_reserved_versions(reservations, "tianshu-backend"), {3, 8, 10})
        self.assertEqual(allocator.parse_reserved_versions(reservations, "tianshu-frontend"), {3, 8, 9})

    def test_noncanonical_target_reservations_are_ignored(self):
        reservations = refs("2", "003", "3/child", "3\n", "+3", "３", "3.0", "2026-11-20")
        self.assertEqual(allocator.parse_reserved_versions(reservations, "tianshu-backend"), set())

    def test_invalid_image_name_is_rejected(self):
        for image_name in ["", "backend", "tianshu-backend/child", "tianshu-frontend:3", "../image"]:
            with self.subTest(image_name=image_name), self.assertRaises(ValueError):
                allocator.parse_reserved_versions([], image_name)


class SelectionTests(unittest.TestCase):
    def test_initial_automatic_version_is_three_and_checks_only_target_image(self):
        for image_name in allocator.IMAGE_NAMES:
            with self.subTest(image_name=image_name):
                exists = mock.Mock(return_value=False)
                self.assertEqual(allocator.select_version("", set(), exists, image_name), "3")
                exists.assert_called_once_with(f"registry.cn-chengdu.aliyuncs.com/tavan-ai/{image_name}:3")

    def test_reserved_high_water_mark_controls_automatic_increment(self):
        for reserved, expected in [({3}, "4"), ({3, 10}, "11"), ({10}, "11")]:
            with self.subTest(reserved=reserved):
                self.assertEqual(
                    allocator.select_version("  ", reserved, lambda image: False, "tianshu-backend"), expected
                )

    def test_manual_ten_reservation_makes_next_automatic_version_eleven(self):
        reserved = {3}
        chosen = allocator.select_version("10", reserved, lambda image: False, "tianshu-backend")
        reserved.add(int(chosen))
        self.assertEqual(allocator.select_version("", reserved, lambda image: False, "tianshu-backend"), "11")

    def test_backend_manual_ten_does_not_advance_frontend_counter(self):
        reservations = refs(3, 10)
        backend = allocator.parse_reserved_versions(reservations, "tianshu-backend")
        frontend = allocator.parse_reserved_versions(reservations, "tianshu-frontend")
        self.assertEqual(allocator.select_version("", backend, lambda image: False, "tianshu-backend"), "11")
        self.assertEqual(allocator.select_version("", frontend, lambda image: False, "tianshu-frontend"), "3")

    def test_unused_manual_number_below_high_water_is_allowed_without_automatic_regression(self):
        reserved = {3, 10}
        self.assertEqual(allocator.select_version("7", reserved, lambda image: False, "tianshu-backend"), "7")
        self.assertEqual(allocator.select_version("", reserved | {7}, lambda image: False, "tianshu-backend"), "11")

    def test_manual_input_trims_whitespace_and_normalizes_leading_zeroes(self):
        self.assertEqual(allocator.select_version("  003  ", set(), lambda image: False, "tianshu-backend"), "3")

    def test_invalid_manual_values_are_rejected_before_registry_requests(self):
        for requested in [
            "0",
            "1",
            "2",
            "-3",
            "+3",
            "3.0",
            "v3",
            "３",
            "٣",
            "3\n4",
            "3; echo bad",
            "$(id)",
            "3/4",
            "3:latest",
            "9" * 129,
            "0" * 128 + "3",
        ]:
            with self.subTest(requested=requested):
                exists = mock.Mock(return_value=False)
                with self.assertRaises(ValueError):
                    allocator.select_version(requested, set(), exists, "tianshu-backend")
                exists.assert_not_called()

    def test_docker_tag_length_limit_accepts_128_digits_but_rejects_automatic_overflow(self):
        maximum = "9" * 128
        self.assertEqual(allocator.select_version(maximum, set(), lambda image: False, "tianshu-backend"), maximum)
        exists = mock.Mock(return_value=False)
        with self.assertRaises(ValueError):
            allocator.select_version("", {int(maximum)}, exists, "tianshu-backend")
        exists.assert_not_called()

    def test_reserved_manual_version_is_rejected_without_registry_requests(self):
        exists = mock.Mock(return_value=False)
        with self.assertRaises(ValueError):
            allocator.select_version("003", {3}, exists, "tianshu-backend")
        exists.assert_not_called()

    def test_manual_version_is_rejected_if_target_image_tag_exists(self):
        for image_name in allocator.IMAGE_NAMES:
            with self.subTest(image_name=image_name):

                def exists(image):
                    return image == f"{allocator.REGISTRY_PREFIX}/{image_name}:7"

                with self.assertRaises(ValueError):
                    allocator.select_version("7", set(), exists, image_name)

    def test_automatic_version_skips_target_collisions_without_checking_other_image(self):
        existing = {
            f"{allocator.REGISTRY_PREFIX}/tianshu-backend:3",
            f"{allocator.REGISTRY_PREFIX}/tianshu-frontend:4",
        }
        exists = mock.Mock(side_effect=existing.__contains__)
        self.assertEqual(allocator.select_version("", set(), exists, "tianshu-backend"), "4")
        self.assertEqual(
            exists.call_args_list,
            [
                mock.call(f"{allocator.REGISTRY_PREFIX}/tianshu-backend:3"),
                mock.call(f"{allocator.REGISTRY_PREFIX}/tianshu-backend:4"),
            ],
        )

    def test_existing_backend_tag_does_not_block_frontend_version_three(self):
        existing = {f"{allocator.REGISTRY_PREFIX}/tianshu-backend:3"}
        self.assertEqual(allocator.select_version("", set(), existing.__contains__, "tianshu-frontend"), "3")
        self.assertEqual(allocator.select_version("3", set(), existing.__contains__, "tianshu-frontend"), "3")

    def test_invalid_image_name_is_rejected_before_registry_requests(self):
        for image_name in ["", "backend", "tianshu-backend/child", "tianshu-frontend:3", "../image"]:
            with self.subTest(image_name=image_name):
                exists = mock.Mock(return_value=False)
                with self.assertRaises(ValueError):
                    allocator.select_version("3", set(), exists, image_name)
                exists.assert_not_called()

    def test_registry_failure_is_propagated_for_manual_and_automatic_selection(self):
        for requested in ["", "7"]:
            with self.subTest(requested=requested):
                failure = RuntimeError("registry authentication unavailable")
                with self.assertRaises(RuntimeError) as raised:
                    allocator.select_version(requested, set(), mock.Mock(side_effect=failure), "tianshu-backend")
                self.assertIs(raised.exception, failure)


class RegistryLookupTests(unittest.TestCase):
    def test_successful_manifest_inspection_means_tag_exists(self):
        image = "registry.example/namespace/image:3"
        with mock.patch.object(
            allocator.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stderr="")
        ) as run:
            self.assertTrue(allocator.registry_tag_exists(image))
        run.assert_called_once_with(
            ["docker", "manifest", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

    def test_only_explicit_missing_manifest_errors_mean_tag_does_not_exist(self):
        for stderr in ["manifest unknown", "no such manifest: image:3"]:
            with self.subTest(stderr=stderr):
                with mock.patch.object(
                    allocator.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stderr=stderr)
                ):
                    self.assertFalse(allocator.registry_tag_exists("image:3"))

    def test_network_authentication_and_other_errors_do_not_mean_missing_tag(self):
        for stderr in [
            "unauthorized: authentication required",
            "denied: access forbidden",
            "dial tcp: connection refused",
            "TLS handshake timeout",
            "no such host",
            "",
            "unknown error",
        ]:
            with self.subTest(stderr=stderr):
                with mock.patch.object(
                    allocator.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stderr=stderr)
                ):
                    with self.assertRaises(RuntimeError):
                        allocator.registry_tag_exists("image:3")

    def test_manifest_inspection_timeout_is_propagated(self):
        failure = subprocess.TimeoutExpired(["docker", "manifest", "inspect"], 60)
        with mock.patch.object(allocator.subprocess, "run", side_effect=failure):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                allocator.registry_tag_exists("image:3")
        self.assertIs(raised.exception, failure)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "output"
        self.summary = Path(self.temp.name) / "summary"
        self.env = {
            "REQUESTED_VERSION": "",
            "IMAGE_NAME": "tianshu-backend",
            "GITHUB_REPOSITORY": "owner/project",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        self.env_patch = mock.patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def assert_no_output(self):
        self.assertFalse(self.output.exists() and self.output.read_text(encoding="utf-8"))
        self.assertFalse(self.summary.exists() and self.summary.read_text(encoding="utf-8"))

    def test_success_reserves_exact_sha_then_emits_version_once(self):
        self.env["REQUESTED_VERSION"] = "010"
        with (
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(allocator, "registry_tag_exists", return_value=False),
            mock.patch.object(
                allocator, "github_request", side_effect=[refs(3), {"ref": allocator.TAG_PREFIX + "tianshu-backend/10"}]
            ) as github,
        ):
            allocator.main()
        self.assertEqual(
            github.call_args_list[0],
            mock.call("GET", "/repos/owner/project/git/matching-refs/tags/image-build-version/"),
        )
        reservation = github.call_args_list[1]
        self.assertEqual(reservation.args[:2], ("POST", "/repos/owner/project/git/refs"))
        payload = reservation.args[2] if len(reservation.args) > 2 else reservation.kwargs["payload"]
        self.assertEqual(payload, {"ref": allocator.TAG_PREFIX + "tianshu-backend/10", "sha": "a" * 40})
        self.assertEqual(github.call_count, 2)
        self.assertEqual(self.output.read_text(encoding="utf-8").splitlines(), ["version=10"])
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn("tianshu-backend:10", summary)
        self.assertNotIn("tianshu-frontend:", summary)

    def test_frontend_main_ignores_backend_high_water_and_reserves_own_namespace(self):
        with (
            mock.patch.dict(os.environ, {"IMAGE_NAME": "tianshu-frontend"}),
            mock.patch.object(allocator, "registry_tag_exists", return_value=False) as registry,
            mock.patch.object(allocator, "github_request", side_effect=[refs(3, 10), {}]) as github,
        ):
            allocator.main()
        reservation = github.call_args
        payload = reservation.args[2] if len(reservation.args) > 2 else reservation.kwargs["payload"]
        self.assertEqual(payload, {"ref": allocator.TAG_PREFIX + "tianshu-frontend/3", "sha": "a" * 40})
        registry.assert_called_once_with(f"{allocator.REGISTRY_PREFIX}/tianshu-frontend:3")
        self.assertEqual(self.output.read_text(encoding="utf-8").splitlines(), ["version=3"])
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn("tianshu-frontend:3", summary)
        self.assertNotIn("tianshu-backend:", summary)

    def test_main_uses_legacy_shared_markers_as_target_baseline(self):
        with (
            mock.patch.object(allocator, "registry_tag_exists", return_value=False),
            mock.patch.object(allocator, "github_request", side_effect=[refs(10, image_name=None), {}]),
        ):
            allocator.main()
        self.assertEqual(self.output.read_text(encoding="utf-8").splitlines(), ["version=11"])

    def test_main_rejects_reserved_manual_version_without_post_or_output(self):
        with (
            mock.patch.dict(os.environ, {"REQUESTED_VERSION": "3"}),
            mock.patch.object(allocator, "registry_tag_exists", return_value=False),
            mock.patch.object(allocator, "github_request", return_value=refs(3)) as github,
        ):
            with self.assertRaises(ValueError):
                allocator.main()
        self.assertEqual(github.call_count, 1)
        self.assertEqual(github.call_args.args[0], "GET")
        self.assert_no_output()

    def test_summary_path_is_optional(self):
        with (
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(allocator, "registry_tag_exists", return_value=False),
            mock.patch.object(
                allocator, "github_request", side_effect=[[], {"ref": allocator.TAG_PREFIX + "tianshu-backend/3"}]
            ),
        ):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            allocator.main()
        self.assertEqual(self.output.read_text(encoding="utf-8").splitlines(), ["version=3"])

    def test_registry_failure_prevents_atomic_reservation_and_output(self):
        with (
            mock.patch.object(allocator, "registry_tag_exists", side_effect=RuntimeError("registry denied")),
            mock.patch.object(allocator, "github_request", return_value=[]) as github,
        ):
            with self.assertRaises(RuntimeError):
                allocator.main()
        self.assertEqual(github.call_count, 1)
        self.assert_no_output()

    def test_get_failure_prevents_post_registry_lookup_and_output(self):
        failure = HTTPError("https://api.github.com/example", 403, "Forbidden", {}, None)
        with (
            mock.patch.object(allocator, "registry_tag_exists") as registry,
            mock.patch.object(allocator, "github_request", side_effect=failure) as github,
        ):
            with self.assertRaises(HTTPError):
                allocator.main()
        self.assertEqual(github.call_count, 1)
        registry.assert_not_called()
        self.assert_no_output()

    def test_atomic_reservation_collision_does_not_emit_output_or_retry_other_version(self):
        failure = HTTPError("https://api.github.com/example", 422, "Reference already exists", {}, None)
        with (
            mock.patch.object(allocator, "registry_tag_exists", return_value=False),
            mock.patch.object(allocator, "github_request", side_effect=[[], failure]) as github,
        ):
            with self.assertRaises(HTTPError):
                allocator.main()
        self.assertEqual(github.call_count, 2)
        self.assertEqual(github.call_args.args[0], "POST")
        self.assert_no_output()


if __name__ == "__main__":
    unittest.main()
