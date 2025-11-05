#    Copyright 2025 Simplyblock.io
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
"""Unit tests for OpenStack Cinder Simplyblock driver."""
import copy
import json
import logging
import unittest
from unittest import mock

from oslo_utils import units
import requests

from cinder import context
from cinder import exception
from cinder.tests.unit import fake_constants as fake
from cinder.tests.unit import fake_volume
from cinder.tests.unit import test
from cinder.volume import configuration as conf
from cinder.volume.drivers.simplyblock.client import SimplyblockAPIException
from cinder.volume.drivers.simplyblock.driver import SimplyblockDriver

LOG = logging.getLogger(__name__)


class SimplyblockDriverTestCase(test.TestCase):
    def setUp(self):
        super(SimplyblockDriverTestCase, self).setUp()
        self.ctxt = context.RequestContext(
            fake.USER_ID, fake.PROJECT_ID, is_admin=True
        )

        # Mock configuration
        self.configuration = mock.Mock(spec=conf.Configuration)
        self.configuration.simplyblock_endpoint = (
            "https://simplyblock-api.example.com/api/v1"
        )
        self.configuration.simplyblock_cluster_secret = "secret"
        self.configuration.simplyblock_cluster_uuid = (
            "a459b320-c97a-4e0e-a90a-6bfa4f03f6c3"
        )
        self.configuration.simplyblock_pool_name = "testing1"
        self.configuration.safe_get.return_value = "simplyblock"

        # Initialize driver
        self.driver = SimplyblockDriver(configuration=self.configuration)

        # Mock requests.Session().request for all HTTP requests
        self.requests_patcher = mock.patch.object(requests.Session, "request")
        self.requests_mock = self.requests_patcher.start()

        # Test data
        volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt, id=fake.VOLUME_TYPE_ID
        )
        self.volume = fake_volume.fake_volume_obj(
            self.ctxt,
            id=fake.VOLUME_ID,
            size=10,
            provider_id=fake.UUID1,
            volume_type=volume_type,
        )

        self.snapshot = mock.Mock()
        self.snapshot.id = fake.SNAPSHOT_ID
        self.snapshot.volume = self.volume
        self.snapshot.volume_size = 10
        self.snapshot.provider_id = fake.UUID2

        self.connector = {
            "host": "test-host",
            "nqn": "nqn.2024-05.test.host",
            "host_ips": ["192.168.1.100"],
        }

    def tearDown(self):
        self.requests_patcher.stop()
        super().tearDown()

    def mock_successful_response(self, response_data=None, status_code=200):
        """Mock a successful API response"""
        if response_data is None:
            response_data = {}

        mock_response = mock.Mock()
        mock_response.status_code = status_code
        mock_response.ok = status_code == 200
        mock_response.text = json.dumps(response_data)
        mock_response.json.return_value = response_data
        self.requests_mock.return_value = mock_response

    def mock_failed_response(self, status_code=500, error_text="Server Error"):
        """Mock a failed API response"""
        mock_response = mock.Mock()
        mock_response.status_code = status_code
        mock_response.ok = False
        mock_response.text = error_text
        mock_response.json.side_effect = ValueError("Not JSON")
        self.requests_mock.return_value = mock_response

    def test_create_volume_with_extra_specs(self):
        """Test volume creation with extra specs."""
        mock_response = {"results": fake.UUID1}
        self.mock_successful_response(mock_response)

        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                'simplyblock:fabric': 'tcp',
                'simplyblock:priority_class': '2',
                'simplyblock:custom_setting': 'custom_value'
            },
        )

        result = self.driver.create_volume(self.volume)

        # Verify API call was made with extra specs parameters
        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs["json"]

        self.assertEqual(request_json['fabric'], 'tcp')
        self.assertEqual(request_json['lvol_priority_class'], 2)
        self.assertEqual(request_json['custom_setting'], 'custom_value')
        self.assertEqual(result, {"provider_id": fake.UUID1})

    def test_create_volume_with_qos_and_extra_specs(self):
        """Test volume creation with both QoS and extra specs."""
        mock_response = {"results": fake.UUID1}
        self.mock_successful_response(mock_response)

        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                "total_iops_sec": "1000",
                "simplyblock:fabric": "TCP",
                "simplyblock:priority_class": "1",
            },
        )

        result = self.driver.create_volume(self.volume)

        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs["json"]

        # Check QoS parameters
        self.assertEqual(request_json["max_rw_iops"], 1000)

        # Check extra specs parameters
        self.assertEqual(request_json['fabric'], 'tcp')
        self.assertEqual(request_json['lvol_priority_class'], 1)

        self.assertEqual(result, {"provider_id": fake.UUID1})

    def test_create_volume_with_rdma_fabric(self):
        """Test volume creation with RDMA fabric."""
        mock_response = {"results": fake.UUID1}
        self.mock_successful_response(mock_response)

        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                'simplyblock:fabric': 'RDMA',
                'simplyblock:priority_class': '3'
            },
        )

        result = self.driver.create_volume(self.volume)

        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs["json"]

        self.assertEqual(request_json['fabric'], 'rdma')
        self.assertEqual(request_json['lvol_priority_class'], 3)
        self.assertEqual(result, {"provider_id": fake.UUID1})

    def test_create_volume_with_invalid_extra_specs(self):
        """Test volume creation with invalid extra specs (must be ignored)."""
        mock_response = {"results": fake.UUID1}
        self.mock_successful_response(mock_response)

        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                'simplyblock:fabric': 'INVALID',  # Should be ignored
                'simplyblock:priority_class': '10',  # Should be ignored
                'simplyblock:valid_param': 'valid_value'
            },
        )

        result = self.driver.create_volume(self.volume)

        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs["json"]

        # Only valid parameters should be included
        self.assertNotIn('fabric', request_json)
        self.assertNotIn('lvol_priority_class', request_json)
        self.assertEqual(request_json['valid_param'], 'valid_value')
        self.assertEqual(result, {"provider_id": fake.UUID1})

    def test_retype_volume_extra_specs_different(self):
        """Test volume retype with different extra specs

        - should return False.
        """
        # Set up original volume type with some extra specs
        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                'simplyblock:fabric': 'tcp',
                'simplyblock:priority_class': '2'
            },
        )

        new_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                'simplyblock:fabric': 'RDMA',  # Different fabric
                'simplyblock:priority_class': '5'
            },
        )

        result, updates = self.driver.retype(
            self.ctxt, self.volume, new_type, {}, None
        )

        # Verify retype was not successful due to different extra specs
        self.assertFalse(result)
        self.assertEqual(updates, {})

    def test_retype_volume_extra_specs_same(self):
        """Test volume retype with identical extra specs

        - should return True.
        """
        # Set up original volume type with some extra specs
        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                'simplyblock:fabric': 'tcp',
                'simplyblock:priority_class': '2',
                'simplyblock:custom_setting': 'same_value'
            },
        )

        new_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                'simplyblock:fabric': 'tcp',  # Same fabric
                'simplyblock:priority_class': '2',  # Same priority
                'simplyblock:custom_setting': 'same_value'  # Same setting
            },
        )

        # Mock successful QoS update
        self.mock_successful_response()

        result, updates = self.driver.retype(
            self.ctxt, self.volume, new_type, {}, None
        )

        # Verify retype was successful since extra specs are identical
        self.assertTrue(result)
        self.assertEqual(updates, {})

        # Verify that QoS update was called
        # (since force_qos_update=True in retype)
        self.requests_mock.assert_called_once()

    def test_retype_volume_extra_specs_none(self):
        """Test volume retype when both old and new have no extra specs

        - should return True.
        """
        # Set up original volume type with no extra specs
        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={},  # No extra specs
        )

        new_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={},  # Also no extra specs
        )

        # Mock successful QoS update
        self.mock_successful_response()

        result, updates = self.driver.retype(
            self.ctxt, self.volume, new_type, {}, None
        )

        # Verify retype was successful since both have no extra specs
        # (both None)
        self.assertTrue(result)
        self.assertEqual(updates, {})

    def test_setup_should_fail_if_simplyblock_client_cant_connect(self):
        """Verify driver setup fails when Simplyblock API is unavailable."""
        self.mock_failed_response(status_code=500,
                                  error_text="Connection failed")

        self.assertRaises(
            exception.VolumeDriverException, self.driver.do_setup, self.ctxt
        )

    def test_setup_should_fail_with_authentication_error(self):
        """Verify driver setup fails with authentication errors."""
        self.mock_failed_response(status_code=401, error_text="Unauthorized")

        self.assertRaises(
            exception.VolumeDriverException, self.driver.do_setup, self.ctxt
        )

    def test_setup_should_succeed(self):
        """Test that driver setup succeeds with valid API response."""
        self.mock_successful_response({"status": "ok"})

        # Should not raise any exception
        self.driver.do_setup(self.ctxt)

        # Verify API call was made
        self.requests_mock.assert_called_once()

        # Correct way to access call arguments
        call_args, call_kwargs = self.requests_mock.call_args
        self.assertEqual(call_args[0], "GET")
        self.assertIn("/api/v1", call_args[1])

    def test_check_for_setup_error_with_missing_pool_name(self):
        """Verify setup error check fails when pool name is missing."""
        self.configuration.simplyblock_pool_name = None

        self.assertRaises(
            exception.VolumeDriverException, self.driver.check_for_setup_error
        )

    def test_check_for_setup_error_with_valid_config(self):
        """Verify setup error check passes with valid configuration."""
        # Should not raise any exception
        self.driver.check_for_setup_error()

    def test_get_driver_options(self):
        """Test that driver options are returned correctly."""
        options = self.driver.get_driver_options()
        self.assertIsNotNone(options)
        self.assertGreater(len(options), 0)

    def test_get_volume_stats_success(self):
        """Test successful retrieval of volume statistics."""
        mock_stats = {
            "results": {
                "stats": [
                    {
                        "size_total": 100 * units.Gi,
                        "size_prov": 50 * units.Gi,
                        "size_free": 50 * units.Gi,
                        "read_io_ps": 1000,
                        "write_io_ps": 2000,
                        "unmap_io_ps": 50,
                    }
                ]
            }
        }
        self.mock_successful_response(mock_stats)

        stats = self.driver.get_volume_stats(refresh=True)

        self.assertEqual(stats["volume_backend_name"], "simplyblock")
        self.assertEqual(stats["vendor_name"], "Simplyblock")
        self.assertEqual(stats["storage_protocol"], "NVMe-oF")
        self.assertEqual(stats["total_capacity_gb"], 100)
        self.assertEqual(stats["free_capacity_gb"], 50)
        self.assertEqual(stats["provisioned_capacity_gb"], 50)
        self.assertEqual(stats["current_iops"], 3050)
        self.assertTrue(stats["QoS_support"])

    def test_get_volume_stats_api_failure(self):
        """Test volume stats handling when API call fails."""
        self.mock_failed_response(status_code=500, error_text="API error")

        stats = self.driver.get_volume_stats(refresh=True)

        self.assertEqual(stats["total_capacity_gb"], 0)
        self.assertEqual(stats["free_capacity_gb"], 0)
        self.assertTrue(stats["QoS_support"])

    def test_create_volume_success(self):
        """Test successful volume creation."""
        mock_response = {"results": fake.UUID1}
        self.mock_successful_response(mock_response)

        # Ensure volume_type is None for this test
        self.volume.volume_type = None

        result = self.driver.create_volume(self.volume)

        # Verify API call was made with correct parameters
        self.requests_mock.assert_called_once()
        call_args, call_kwargs = self.requests_mock.call_args
        self.assertEqual(call_args[0], "POST")
        self.assertIn("/lvol", call_args[1])

        # Verify request payload
        request_json = call_kwargs["json"]
        self.assertEqual(
            request_json["name"], f"cinder-vol-{self.volume.name_id}"
        )
        self.assertEqual(request_json["size"], f"{self.volume.size}GiB")
        # Verify QoS parameters are absent
        self.assertNotIn("max_rw_iops", request_json)
        self.assertNotIn("max_r_mbytes", request_json)

        self.assertEqual(result, {"provider_id": fake.UUID1})

    def test_create_volume_with_qos(self):
        """Test volume creation with QoS settings."""
        mock_response = {"results": fake.UUID1}
        self.mock_successful_response(mock_response)

        # Create volume with QoS specs in volume type
        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                "total_iops_sec": "1000",
                "read_bytes_sec": "100000000",
            },
        )

        result = self.driver.create_volume(self.volume)

        # Verify API call was made with QoS parameters
        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs["json"]

        self.assertEqual(request_json["max_rw_iops"], 1000)
        self.assertEqual(request_json["max_r_mbytes"], 100)
        self.assertEqual(result, {"provider_id": fake.UUID1})

    def test_create_volume_with_complex_qos(self):
        """Test volume creation with multiple QoS settings."""
        mock_response = {"results": fake.UUID1}
        self.mock_successful_response(mock_response)

        self.volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                "total_iops_sec": "2000",
                "read_bytes_sec": "100000000",
                "write_bytes_sec": "200000000",
                "total_bytes_sec": "300000000",
            },
        )

        result = self.driver.create_volume(self.volume)

        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs["json"]

        self.assertEqual(request_json["max_rw_iops"], 2000)
        self.assertEqual(request_json["max_r_mbytes"], 100)
        self.assertEqual(request_json["max_w_mbytes"], 200)
        self.assertEqual(request_json["max_rw_mbytes"], 300)
        self.assertEqual(result, {"provider_id": fake.UUID1})

    def test_delete_volume_success(self):
        """Test successful volume deletion."""
        self.mock_successful_response()

        self.driver.delete_volume(self.volume)

        # Verify API call was made
        self.requests_mock.assert_called_once()
        call_args, call_kwargs = self.requests_mock.call_args
        self.assertEqual(call_args[0], "DELETE")
        self.assertIn(f"/lvol/{self.volume.provider_id}", call_args[1])

    def test_delete_volume_no_provider_id(self):
        """Test volume deletion when provider_id is missing."""
        volume = copy.deepcopy(self.volume)
        volume.provider_id = None

        self.driver.delete_volume(volume)

        # Verify no API call was made
        self.requests_mock.assert_not_called()

    def test_extend_volume_success(self):
        """Test successful volume extension."""
        self.mock_successful_response()

        new_size = 20
        self.driver.extend_volume(self.volume, new_size)

        # Verify API call was made
        self.requests_mock.assert_called_once()
        call_args, call_kwargs = self.requests_mock.call_args
        self.assertEqual(call_args[0], "PUT")
        self.assertIn(f"/lvol/resize/{self.volume.provider_id}", call_args[1])

        # Verify request payload
        request_json = call_kwargs["json"]
        self.assertEqual(request_json["size"], f"{new_size}G")

    def test_initialize_connection_success(self):
        """Test successful volume connection initialization."""
        mock_response = {
            "results": [
                {
                    "ip": "192.168.1.10",
                    "port": 4420,
                    "nqn": "nqn.2024-05.simplyblock:clus-id:lvol:vol-123",
                    "transport": "tcp",
                }
            ]
        }
        self.mock_successful_response(mock_response)

        conn_info = self.driver.initialize_connection(
            self.volume, self.connector
        )

        expected_data = {
            "target_nqn": "nqn.2024-05.simplyblock:clus-id:lvol:vol-123",
            "portals": [("192.168.1.10", 4420, "tcp")],
            "host_nqn": self.connector["nqn"],
            "volume_id": self.volume.id,
            "access_mode": "rw",
            "discard": False,
            "vol_uuid": self.volume.provider_id,
        }

        self.assertEqual(conn_info["driver_volume_type"], "nvmeof")
        self.assertDictEqual(conn_info["data"], expected_data)

    def test_initialize_connection_no_provider_id(self):
        """Test connection initialization when provider_id is missing."""
        volume = copy.deepcopy(self.volume)
        volume.provider_id = None

        self.assertRaises(
            exception.VolumeDriverException,
            self.driver.initialize_connection,
            volume,
            self.connector,
        )

    def test_terminate_connection(self):
        """Test that terminate_connection doesn't raise exceptions."""
        # Should not raise any exception
        self.driver.terminate_connection(self.volume, self.connector)

    def test_create_snapshot_success(self):
        """Test successful snapshot creation."""
        mock_response = {"results": fake.UUID2}
        self.mock_successful_response(mock_response)

        result = self.driver.create_snapshot(self.snapshot)

        # Verify API call was made
        self.requests_mock.assert_called_once()
        call_args, call_kwargs = self.requests_mock.call_args
        self.assertEqual(call_args[0], "POST")
        self.assertIn("/snapshot", call_args[1])

        # Verify request payload
        request_json = call_kwargs["json"]
        self.assertEqual(request_json["lvol_id"], self.volume.provider_id)
        self.assertEqual(
            request_json["snapshot_name"], f"cinder-snap-{self.snapshot.id}"
        )

        self.assertEqual(result, {"provider_id": fake.UUID2})

    def test_delete_snapshot_success(self):
        """Test successful snapshot deletion."""
        self.mock_successful_response()

        self.driver.delete_snapshot(self.snapshot)

        # Verify API call was made
        self.requests_mock.assert_called_once()
        call_args, call_kwargs = self.requests_mock.call_args
        self.assertEqual(call_args[0], "DELETE")
        self.assertIn(f"/snapshot/{self.snapshot.provider_id}", call_args[1])

    def test_create_volume_from_snapshot_success(self):
        """Test successful volume creation from snapshot."""
        mock_response = {"results": fake.UUID3}
        self.mock_successful_response(mock_response)

        new_volume = copy.deepcopy(self.volume)
        new_volume.id = fake.VOLUME2_ID
        new_volume.provider_id = None
        new_volume.volume_type = None

        result = self.driver.create_volume_from_snapshot(
            new_volume, self.snapshot
        )

        # Verify API call was made
        self.requests_mock.assert_called_once()
        call_args, call_kwargs = self.requests_mock.call_args
        self.assertEqual(call_args[0], "POST")
        self.assertIn("/snapshot/clone", call_args[1])

        # Verify request payload
        request_json = call_kwargs["json"]
        self.assertEqual(
            request_json["snapshot_id"],
            self.snapshot.provider_id
        )
        self.assertEqual(
            request_json["clone_name"],
            f"cinder-vol-{new_volume.name_id}"
        )

        self.assertEqual(result, {"provider_id": fake.UUID3})

    def test_create_volume_from_snapshot_with_qos(self):
        """Test volume creation from snapshot with QoS settings."""
        mock_response = {"results": fake.UUID3}
        self.mock_successful_response(mock_response)

        new_volume = copy.deepcopy(self.volume)
        new_volume.id = fake.VOLUME2_ID
        new_volume.provider_id = None
        new_volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                "total_iops_sec": "1500",
                "write_bytes_sec": "200000000",
            },
        )

        result = self.driver.create_volume_from_snapshot(
            new_volume,
            self.snapshot
        )

        # Verify API calls were made
        self.requests_mock.assert_called()
        self.assertEqual(result, {"provider_id": fake.UUID3})

    def test_create_cloned_volume_success(self):
        """Test successful volume cloning."""
        mock_snapshot_response = {"results": "tmp-snap-123"}
        mock_clone_response = {"results": fake.UUID3}

        # Mock sequence of API calls
        self.requests_mock.side_effect = [
            mock.Mock(
                **{
                    "status_code": 200,
                    "ok": True,
                    "text": json.dumps(mock_snapshot_response),
                    "json.return_value": mock_snapshot_response,
                }
            ),
            mock.Mock(
                **{
                    "status_code": 200,
                    "ok": True,
                    "text": json.dumps(mock_clone_response),
                    "json.return_value": mock_clone_response,
                }
            ),
            mock.Mock(
                **{
                    "status_code": 200,
                    "ok": True,
                    "text": "{}",
                    "json.return_value": {},
                }
            ),
        ]

        new_volume = copy.deepcopy(self.volume)
        new_volume.id = fake.VOLUME2_ID
        new_volume.provider_id = None
        new_volume.volume_type = None

        result = self.driver.create_cloned_volume(new_volume, self.volume)

        # Verify all API calls were made
        self.assertEqual(self.requests_mock.call_count, 3)

        self.assertEqual(result, {"provider_id": fake.UUID3})

    def test_create_cloned_volume_with_qos(self):
        """Test volume cloning with QoS settings."""
        mock_snapshot_response = {"results": "tmp-snap-123"}
        mock_clone_response = {"results": fake.UUID3}

        self.requests_mock.side_effect = [
            mock.Mock(
                **{
                    "status_code": 200,
                    "ok": True,
                    "text": json.dumps(mock_snapshot_response),
                    "json.return_value": mock_snapshot_response,
                }
            ),
            mock.Mock(
                **{
                    "status_code": 200,
                    "ok": True,
                    "text": json.dumps(mock_clone_response),
                    "json.return_value": mock_clone_response,
                }
            ),
            mock.Mock(
                **{
                    "status_code": 200,
                    "ok": True,
                    "text": "{}",
                    "json.return_value": {},
                }
            ),
            mock.Mock(
                **{
                    "status_code": 200,
                    "ok": True,
                    "text": "{}",
                    "json.return_value": {},
                }
            ),
        ]

        new_volume = copy.deepcopy(self.volume)
        new_volume.id = fake.VOLUME2_ID
        new_volume.provider_id = None
        new_volume.volume_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                "total_bytes_sec": "300000000",
            },
        )

        result = self.driver.create_cloned_volume(new_volume, self.volume)

        self.assertEqual(result, {"provider_id": fake.UUID3})

    def test_create_cloned_volume_failure(self):
        """Test volume cloning failure during snapshot creation."""
        self.mock_failed_response(status_code=500, error_text="API error")

        new_volume = copy.deepcopy(self.volume)
        new_volume.id = fake.VOLUME2_ID
        new_volume.provider_id = None

        self.assertRaises(
            exception.VolumeDriverException,
            self.driver.create_cloned_volume,
            new_volume,
            self.volume,
        )

    def test_retype_volume_qos(self):
        """Test volume retype with new QoS settings."""
        new_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                "total_iops_sec": "4000",
                "read_bytes_sec": "100000000",
            },
        )

        # Mock successful QoS update
        self.mock_successful_response()

        result, updates = self.driver.retype(
            self.ctxt, self.volume, new_type, {}, None
        )

        # Verify retype was successful
        self.assertTrue(result)
        self.assertEqual(updates, {})

        # Verify QoS update was called
        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs["json"]
        self.assertEqual(request_json["max_rw_iops"], 4000)
        self.assertEqual(request_json["max_r_mbytes"], 100)

    def test_retype_volume_qos_failure(self):
        """Test volume retype failure when QoS update fails."""
        new_type = fake_volume.fake_volume_type_obj(
            self.ctxt,
            extra_specs={
                "total_iops_sec": "4000",
            },
        )

        # Mock failed QoS update
        with (mock.patch.object(self.driver.client, "update_volume")
              as mock_update):
            mock_update.side_effect = \
                SimplyblockAPIException(message="QoS error")

            self.assertRaises(
                SimplyblockAPIException,
                self.driver.retype,
                self.ctxt,
                self.volume,
                new_type,
                {},
                None,
            )

    def test_manage_existing_success(self):
        """Test successful manage_existing flow."""
        backend_id = fake.UUID3
        # 1) backend get response
        get_resp = {
            "results": [
                {
                    "id": backend_id,
                    "uuid": backend_id,
                    "size_gb": 10000000,
                    "lvol_name": "old-backend-name"
                },
            ],
            "status": True
        }
        mock_get = mock.Mock(
            status_code=200,
            ok=True,
            text=json.dumps(get_resp),
            json=mock.Mock(return_value=get_resp),
        )
        # 2) backend update response
        upd_resp = {}
        mock_update = mock.Mock(
            status_code=200,
            ok=True,
            text=json.dumps(upd_resp),
            json=mock.Mock(return_value=upd_resp),
        )

        # requests.Session.request is patched globally;
        # ensure two-call sequence
        self.requests_mock.side_effect = [mock_get, mock_update]

        existing_ref = {"source-id": backend_id}
        result = self.driver.manage_existing(self.volume, existing_ref)

        # provider_id must be set and result must contain it
        self.assertEqual(result, {"provider_id": backend_id})
        self.assertEqual(self.volume.provider_id, backend_id)

        # verify update call payload (last call)
        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs.get("json", {})
        self.assertEqual(
            request_json.get("name"), f"cinder-vol-{self.volume.name_id}"
        )
        self.assertIn("attributes", request_json)
        self.assertEqual(
            request_json["attributes"].get("uuid"), self.volume.id
        )

    def test_manage_existing_get_size_success(self):
        """Test manage_existing_get_size returns correct GB size."""
        backend_id = fake.UUID3
        resp = {
            "results": [
                {
                    "id": backend_id,
                    "uuid": backend_id,
                    "size": 12000000000,
                }
            ],
            "status": True,
        }
        self.mock_successful_response(resp)
        size = self.driver.manage_existing_get_size(
            self.volume, {"source-id": backend_id}
        )
        self.assertEqual(size, 12)

    def test_unmanage_success(self):
        """Test successful unmanage flow.

        Restore old name and clear imported attrs.
        """
        backend_id = fake.UUID3

        get_resp = {
            "results": [
                {
                    "id": backend_id,
                    "lvol_name": f"cinder-vol-{self.volume.name_id}",
                    "attributes": {
                        "uuid": self.volume.id,
                        "old_name": "orig-backend-name",
                        "is_clone": "False",
                        "os_imported_at": "2025-09-30T12:00:00",
                        # maybe there are other attrs that should survive:
                        "some_other": "keep-me"
                    },
                }
            ],
            "status": True,
        }
        mock_get = mock.Mock(
            status_code=200,
            ok=True,
            text=json.dumps(get_resp),
            json=mock.Mock(return_value=get_resp),
        )

        upd_resp = {}
        mock_update = mock.Mock(
            status_code=200,
            ok=True,
            text=json.dumps(upd_resp),
            json=mock.Mock(return_value=upd_resp),
        )

        # Ensure request sequence: GET, PUT
        self.requests_mock.side_effect = [mock_get, mock_update]

        # set provider_id on cinder volume to simulate managed volume
        self.volume.provider_id = backend_id

        # call unmanage
        self.driver.unmanage(self.volume)

        # verify last request payload
        call_args, call_kwargs = self.requests_mock.call_args
        request_json = call_kwargs.get("json", {})
        # We expect name reset to original
        # and attributes to NOT include imported uuid
        self.assertEqual(request_json.get("name"), "orig-backend-name")
        self.assertNotIn("uuid", request_json.get("attributes", {}))
        self.assertNotIn("old_name", request_json.get("attributes", {}))
        # other attributes should survive
        self.assertEqual(
            request_json.get("attributes", {}).get("some_other"), "keep-me"
        )

    def test_unmanage_no_provider_id(self):
        """Test umanage without provider_id.

        If volume has no provider_id,
        unmanage should be a no-op (no HTTP calls).
        """
        # Clear provider_id
        self.volume.provider_id = None

        # Ensure no exception and no outbound request
        self.driver.unmanage(self.volume)
        # requests.Session.request should not have been called
        self.requests_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
