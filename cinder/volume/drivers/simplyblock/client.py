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
# A minimal Simplyblock REST API client for use with an OpenStack Cinder
# driver.
import logging
from typing import Any, Dict, List

import requests

from cinder.exception import VolumeBackendAPIException
from cinder.utils import retry


LOG = logging.getLogger(__name__)


class SimplyblockAPIException(VolumeBackendAPIException):
    """Raised for any Simplyblock client errors."""


retry_exc_tuple = (SimplyblockAPIException,)


class SimplyblockClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        pool_name: str,
        verify_ssl: bool = True,
        timeout: int = 30,
    ):
        """Init Simplyblock client.

        :param base_url: Base API endpoint,
            e.g. https://mycluster.simplyblock.io/api/v1
        :param api_token: API token string
        :param pool_name: Simplyblock pool name
        :param verify_ssl: Verify SSL certs
        :param timeout: Default HTTP timeout in seconds
        """
        LOG.debug(
            "Initialising Simplyblock client with parameters: "
            "base_url: %s, api_token: %s"
            "pool_name: %s, verify_ssl: %s, "
            "timeout: %s",
            base_url, api_token, pool_name, verify_ssl, timeout
        )
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()

        self.session.headers.update(
            {"Authorization": f"{self.api_token}",
             "Content-Type": "application/json"}
        )
        self.pool_name = pool_name

    def _url(self, path: str) -> str:
        return (
            f"{self.base_url}{path}"
            if path != "/"
            else self.base_url.replace("api/v1/", "")
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = self._url(path)
        LOG.debug("%s: %s    %s", method, url, kwargs.get("json"))
        try:
            resp = self.session.request(
                method, url, verify=self.verify_ssl, timeout=self.timeout,
                **kwargs
            )
        except requests.RequestException as e:
            raise SimplyblockAPIException(f"HTTP request failed: {e}")

        if 'iostats' not in path:
            LOG.debug("%s: %s", resp.status_code, resp.text)
        if not resp.ok:
            raise SimplyblockAPIException(f"{resp.status_code}: {resp.text}")

        if resp.text:
            try:
                return resp.json()
            except ValueError:
                return resp.text
        return None

    # --- API methods ---
    def list_volumes(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/lvol")

    def get_volume(self, volume_id: str) -> Dict[str, Any]:
        try:
            res = self._request("GET", f"/lvol/{volume_id}")
            LOG.warning('_request returned %s', res)
            lvol = res["results"][0]
            LOG.warning('lvol %s', res)
        except Exception:
            raise SimplyblockAPIException

        return lvol

    def create_volume(self, name: str, size_gib: int, **kwargs) \
            -> Dict[str, Any]:
        data = {
            "name": name,
            "size": f"{size_gib}GiB",
            "pool": self.pool_name,
        }
        data.update(kwargs)
        return self._request("POST", "/lvol", json=data)

    def update_volume(self, volume_id: str, data, **kwargs) -> Dict[str, Any]:
        return self._request("PUT", f"/lvol/{volume_id}", json=data)

    @retry(retry_exc_tuple, interval=2, retries=3)
    def delete_volume(self, volume_id: str) -> None:
        self._request("DELETE", f"/lvol/{volume_id}")

    def extend_volume(self, volume_id: str, new_size_gib: str) \
            -> Dict[str, Any]:
        data = {"size": new_size_gib}
        return self._request("PUT", f"/lvol/resize/{volume_id}", json=data)

    def get_cluster_stats(self, cluster_id: str):
        return self._request("GET", f"/cluster/iostats/{cluster_id}")

    def get_volume_connection_strings(self, volume_id: str):
        return self._request("GET", f"/lvol/connect/{volume_id}")

    def map_volume(self, volume_id: str, host_id: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/lvol/{volume_id}/map", json={"host_id": host_id}
        )

    def unmap_volume(self, volume_id: str, host_id: str) -> None:
        self._request(
            "POST", f"/lvol/{volume_id}/unmap", json={"host_id": host_id}
        )

    def status_check(self) -> Dict[str, Any]:
        return self._request("GET", "/")

    def create_snapshot(self, volume_id: str, name: str) -> Dict[str, Any]:
        """Create a snapshot from volume.

        Args:
            volume_id: ID of source volume
            snapshot_id: ID for new snapshot

        Returns:
            Dictionary with snapshot details
        """
        data = {"lvol_id": volume_id, "snapshot_name": name}
        return self._request("POST", "/snapshot", json=data)

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete a snapshot.

        Args:
            snapshot_id: ID of snapshot to delete
        """
        self._request("DELETE", f"/snapshot/{snapshot_id}")

    def clone_volume_from_snapshot(
        self, src_snapshot_id: str, name: str, new_size_gib: int = None
    ) -> Dict[str, Any]:
        """Create new volume from snapshot.

        Args:
            src_snapshot_id: Source snapshot ID
            clone_name: Name of the clone volume

        Returns:
            Dictionary with new volume details
        """
        data = {"snapshot_id": src_snapshot_id, "clone_name": name}
        if new_size_gib:
            data["new_size"] = f"{new_size_gib}GiB"
        return self._request("POST", "/snapshot/clone", json=data)

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """List all snapshots.

        Returns:
            List of snapshot dictionaries
        """
        return self._request("GET", "/snapshot")

    def get_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        """Get snapshot details.

        Args:
            snapshot_id: ID of snapshot

        Returns:
            Dictionary with snapshot details
        """
        return self._request("GET", f"/snapshot/{snapshot_id}")
