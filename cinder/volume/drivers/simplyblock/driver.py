# simplyblock_driver.py — Part 1
#
# OpenStack Cinder Volume Driver for Simplyblock (NVMe/TCP)
#
# SPDX-License-Identifier: Apache-2.0

from typing import Any, Dict
import math

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units

from cinder import context, exception, interface
from cinder.common import constants
from cinder.i18n import _
from cinder.volume import driver, qos_specs
from .client import SimplyblockAPIException, SimplyblockClient


LOG = logging.getLogger(__name__)

simplyblock_opts = [
    cfg.StrOpt("simplyblock_endpoint", help="Simplyblock API base URL"),
    cfg.StrOpt("simplyblock_cluster_secret", help="Simplyblock Cluster secret"),
    cfg.StrOpt("simplyblock_cluster_uuid", help="Cluster UUID"),
    cfg.StrOpt("simplyblock_pool_name", help="Simplyblock Pool Name"),
]
CONF = cfg.CONF
CONF.register_opts(simplyblock_opts, group="simplyblock")


class SimplyblockDriverException(exception.VolumeDriverException):
    message = _("Simplyblock Cinder driver failure: %(reason)s")


class SimplyblockRetryableException(exception.VolumeBackendAPIException):
    message = _("Retryable Simplyblock Exception encountered")


@interface.volumedriver
class SimplyblockDriver(driver.VolumeDriver):
    """OpenStack cinder driver to enable Simplyblock cluster via NVMe/TCP.

    .. code-block:: default

      Version history:
          0.1.0 - Initial driver

    """

    VERSION = "0.1.0"

    # ThirdPartySystems wiki page
    CI_WIKI_NAME = "Simplyblock_CI"

    # Supported QoS settings
    QOS_KEY_MAP = {
        "total_iops_sec": "max_rw_iops",
        "read_bytes_sec": "max_r_mbytes",
        "write_bytes_sec": "max_w_mbytes",
        "total_bytes_sec": "max_rw_mbytes",
    }

    retry_exc_tuple = (SimplyblockRetryableException, SimplyblockAPIException)

    def __init__(self, *args, **kwargs):
        super(SimplyblockDriver, self).__init__(*args, **kwargs)

        self.transport_type = "tcp"
        self._storage_protocol = constants.NVMEOF_TCP
        self.configuration = kwargs.get("configuration", None) or CONF.simplyblock
        api_token = f"{self.configuration.simplyblock_cluster_uuid} {self.configuration.simplyblock_cluster_secret}"
        LOG.debug(f"Initialising Simplyblock volume driver")
        self.client = SimplyblockClient(
            base_url=self.configuration.simplyblock_endpoint,
            api_token=api_token,
            pool_name=self.configuration.simplyblock_pool_name,
        )

    @classmethod
    def get_driver_options(cls):
        additional_opts = cls._get_oslo_driver_opts("volume_backend_name")
        return simplyblock_opts + additional_opts

    def do_setup(self, context):
        """Validate connection to Simplyblock API."""
        LOG.info(
            "Connecting to Simplyblock API at %s",
            self.configuration.simplyblock_endpoint,
        )
        try:
            self.client.status_check()
        except SimplyblockAPIException as e:
            LOG.error("Simplyblock API ping failed.")
            raise exception.VolumeDriverException(
                message=f"Failed to connect to Simplyblock API: {e}"
            )

    def check_for_setup_error(self):
        """Ensure driver is ready to serve requests."""
        if not self.configuration.simplyblock_pool_name:
            raise exception.VolumeDriverException(
                message="simplyblock_pool_name must be configured"
            )

    # ---------------- Stats & Capabilities ----------------
    def get_volume_stats(self, refresh=False):
        """Get volume status.

        If 'refresh' is True, run update first.
        The name is a bit misleading as
        the majority of the data here is cluster
        data
        """
        LOG.debug("Getting volume stats (refresh=%s)", refresh)
        if refresh:
            try:
                self._update_cluster_stats()
            except SimplyblockDriverException:
                LOG.debug("Getting volume stats (refresh=%s)", refresh)

        LOG.debug("Simplyblock cluster_stats: %s", self.cluster_stats)
        return self.cluster_stats

    def _update_cluster_stats(self):
        """Retrieve status info for the Cluster."""
        LOG.debug("Updating Simplyblock cluster stats...")

        backend_name = self.configuration.safe_get("volume_backend_name")

        data = {
            "volume_backend_name": backend_name or self.__class__.__name__,
            "vendor_name": "Simplyblock",
            "driver_version": self.VERSION,
            "storage_protocol": self._storage_protocol,
            "QoS_support": True,
            "multiattach": True,
            "online_extend_support": True,
        }

        try:
            results = self.client.get_cluster_stats(
                self.configuration.simplyblock_cluster_uuid
            )
        except SimplyblockAPIException:
            LOG.error("Could not get simplyblock cluster stats")
            data["total_capacity_gb"] = 0
            data["free_capacity_gb"] = 0
            self.cluster_stats = data
            return

        results = results["results"]["stats"][0]
        data["total_capacity_gb"] = results["size_total"] / units.G
        data["thin_provisioning_support"] = False
        data["provisioned_capacity_gb"] = results["size_prov"] / units.G
        data["free_capacity_gb"] = results["size_free"] / units.G

        data["current_iops"] = (
            results["read_io_ps"] + results["write_io_ps"] + results["unmap_io_ps"]
        )

        data["shared_targets"] = False
        self.cluster_stats = data

    # simplyblock_driver.py — Part 2
    #
    # Volume lifecycle and connection methods

    def _get_qos_settings(self, volume_type):
        """Get extra_specs and qos_specs of a volume_type.

        This fetches the keys from the volume type. Anything set
        from qos_specs will override keys set from extra_specs
        """
        if not volume_type:
            return None

        qos_specs_id = volume_type.get("qos_specs_id")
        if qos_specs_id is not None:
            ctxt = context.get_admin_context()
            specs = qos_specs.get_qos_specs(ctxt, qos_specs_id)
            LOG.debug("qos_specs: %s", specs)
            if specs["consumer"] not in ("back-end", "both"):
                return None
            kvs = specs["specs"]
        else:
            kvs = volume_type.get("extra_specs", {})

        qos = {}
        for key, value in kvs.items():
            if key in self.QOS_KEY_MAP:
                sb_key = self.QOS_KEY_MAP[key]
                try:
                    # Convert string to int first
                    int_value = int(value)
                except (ValueError, TypeError):
                    LOG.warning("Invalid QoS value %s for key %s", value, key)
                    continue

                if "mbytes" in sb_key and "bytes" in key:
                    int_value = int_value // units.M
                qos[sb_key] = int_value

        if qos == {}:
            return None

        LOG.debug("qos_specs in simplyblock format: %s", qos)
        return qos

    def create_volume(self, volume):
        """Create a new volume."""
        LOG.info("Creating volume %s of size %d GB", volume.name_id, volume.size)
        payload = {
            "name": f"cinder-vol-{volume.name_id}",
            "size_gb": volume.size,
        }
        qos = self._get_qos_settings(volume.volume_type)
        if qos:
            LOG.debug("Got QoS settings for volume %s: %s", volume.name_id, qos)
            payload.update(qos)
        lv = self.client.create_volume(**payload)
        # Save volume ID in provider_id so we can find it later
        volume.provider_id = lv.get("results")
        LOG.info(
            f"Volume %s created successfully. Provider id: %s",
            volume.name_id,
            volume.provider_id,
        )
        return {"provider_id": volume.provider_id}

    def _extend_if_needed(self, volume, old_size, new_size):
        """Extend the volume from size src_size to size vol_size."""
        if new_size > old_size:
            new_size_str = f"{new_size}G"
            self.client.extend_volume(volume.provider_id, new_size_str)

    def _setup_volume(
        self, volume, volume_type=None, raise_on_error=False, force_qos_update=False
    ):
        if not volume_type:
            volume_type = volume.volume_type

        qos = self._get_qos_settings(volume_type)
        if not qos:
            if force_qos_update:
                qos = {}
            else:
                return

        LOG.info("Setting qos %s for volume %s", qos, volume.name_id)
        try:
            self.client.update_volume(volume.provider_id, data=qos)
        except SimplyblockAPIException as e:
            LOG.error(
                "Failed to apply qos settings %s for volume %s: %s",
                qos,
                volume,
                e.message,
            )
            if raise_on_error:
                raise

        LOG.info("Qos %s for volume %s updated.", qos, volume.name_id)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create a new volume from a snapshot."""
        LOG.info("Creating volume %s from snapshot %s", volume.name_id, snapshot.id)

        new_size_gb = volume.size
        res = self.client.clone_volume_from_snapshot(
            src_snapshot_id=snapshot.provider_id,
            name=f"cinder-vol-{volume.name_id}",
            new_size_gb=new_size_gb,
        )
        volume.provider_id = res.get("results")

        # apply qos
        self._setup_volume(volume)

        LOG.info(
            f"Volume %s from snapshot %s created successfully. " f"Provider id: %s",
            volume.name_id,
            snapshot.id,
            volume.provider_id,
        )
        return {"provider_id": volume.provider_id}

    def delete_volume(self, volume):
        """Delete a volume."""
        vol_id = volume.provider_id
        if not vol_id:
            LOG.warning("No provider_id for volume %s, skipping delete", volume.name_id)
            return
        LOG.info("Deleting volume %s", vol_id)
        try:
            self.client.delete_volume(vol_id)
        except Exception as e:
            LOG.error("Failed to delete volume %s: %s", vol_id, e)

    def extend_volume(self, volume, new_size):
        """Extend a volume to new_size in GB."""
        vol_id = volume.provider_id
        if not vol_id:
            raise SimplyblockDriverException(
                "Missing provider_id for volume extend"
            )
        LOG.info("Extending volume %s to size %d GB", vol_id, new_size)
        new_size_str = f"{new_size}G"
        self.client.extend_volume(vol_id, new_size_str)

    def initialize_connection(self, volume, connector, **kwargs):
        hostnqn = connector.get("nqn")
        found_dsc = connector.get("found_dsc")
        host_ips = connector.get("host_ips", [])
        LOG.info("Current host hostNQN is %s and IP(s) are %s", hostnqn, host_ips)
        LOG.debug(
            "initialize_connection: connector hostnqn is %s found_dsc %s",
            hostnqn,
            found_dsc,
        )

        vol_id = volume.provider_id
        if not vol_id:
            raise exception.VolumeDriverException("Missing provider_id")

        response = self.client.get_volume_connection_strings(vol_id)
        portals = [
            (p["ip"], p["port"], self.transport_type) for p in response["results"]
        ]

        # multiple portals o the same controller are not supported in os-bricks 2025.1
        # provide only primary connection to avoid errors
        portals = portals[:1]
        target_nqn = response["results"][-1]["nqn"]
        props = {
            "target_nqn": target_nqn,
            "portals": portals,
            "host_nqn": hostnqn,
            "transport_type": "tcp",
            "volume_id": volume["id"],
            "access_mode": "rw",
            "discard": False,
            "vol_uuid": volume.provider_id,
        }
        return {"driver_volume_type": "nvmeof", "data": props}

    def terminate_connection(self, volume, connector, **kwargs):
        pass

    def ensure_export(self, context, volume):
        pass

    def create_export(self, context, volume, connector):
        pass

    # simplyblock_driver.py — Part 3
    #
    # Snapshot, clone, stats, and housekeeping methods

    # ---------------- Snapshot Operations ----------------
    def create_snapshot(self, snapshot):
        """Create a snapshot from a volume."""
        LOG.info("Creating snapshot %s from volume %s", snapshot.id, snapshot.volume_id)
        res = self.client.create_snapshot(
            volume_id=snapshot.volume.provider_id, name=f"cinder-snap-{snapshot.id}"
        )
        snapshot.provider_id = res.get("results")
        LOG.info(
            f"Snapshot {snapshot.id} from volume {snapshot.volume_id} created successfully. "
            f"Provider id: {snapshot.provider_id}"
        )
        return {"provider_id": snapshot.provider_id}

    def delete_snapshot(self, snapshot):
        """Delete a snapshot."""
        LOG.info("Deleting snapshot %s. Provider id: snapshot.provider_id", snapshot.id)
        self.client.delete_snapshot(snapshot.provider_id)

    # ---------------- Housekeeping ----------------
    def initialize_connection_snapshot(self, snapshot, connector, **kwargs):
        """Not implemented: snapshots cannot be directly attached."""
        raise NotImplementedError()

    def terminate_connection_snapshot(self, snapshot, connector, **kwargs):
        """Not implemented: snapshots cannot be directly detached."""
        raise NotImplementedError()

    def _build_nvme_data(self, export: Dict[str, Any]) -> Dict[str, Any]:
        """Build NVMe-oF connection data dictionary from Simplyblock export.

        Args:
            export: Dictionary containing NVMe export details from Simplyblock API.
                   Expected keys: target_nqn, portal_ip, portal_port
                   Optional keys: transport_type, host_nqn

        Returns:
            Dictionary with NVMe connection parameters in Cinder format.

        Raises:
            VolumeDriverException: If required fields are missing in export data.
        """
        required_fields = ["target_nqn", "portal_ip", "portal_port"]
        missing_fields = [field for field in required_fields if field not in export]

        if missing_fields:
            raise exception.VolumeDriverException(
                f"Missing required fields in export data: {missing_fields}"
            )

        # Default to TCP if transport type not specified
        transport_type = export.get("transport_type", "tcp")

        # Validate transport type
        if transport_type.lower() not in ("tcp", "rdma"):
            LOG.warning(
                f"Unsupported transport type: {transport_type}. Defaulting to 'tcp'"
            )
            transport_type = "tcp"

        nvme_data = {
            "target_nqn": export["target_nqn"],
            "target_portal": f"{export['portal_ip']}:{export['portal_port']}",
            "transport_type": transport_type,
        }

        # Add host NQN if provided (for initiator configuration)
        if "host_nqn" in export:
            nvme_data["host_nqn"] = export["host_nqn"]

        LOG.debug("Generated NVMe connection data: %s", nvme_data)
        return nvme_data

    def create_cloned_volume(self, volume, src_vref):
        """Create a clone of the specified volume."""
        LOG.info(
            f"Creating clone of volume %s (provider_id %s) as a new volume %s",
            src_vref.name_id,
            src_vref.provider_id,
            volume.name_id,
        )
        try:
            # 1. Create temporary snapshot from source volume
            res = self.client.create_snapshot(
                volume_id=src_vref.provider_id,
                name=f"cinder-tmp-snap-vol-{src_vref.name_id}-{volume.name_id}",
            )
            snapshot_provider_id = res.get("results")

            # 2. Create new volume from snapshot
            res = self.client.clone_volume_from_snapshot(
                src_snapshot_id=snapshot_provider_id,
                name=f"cinder-vol-{volume.name_id}",
            )

            # 3. Save provider location
            volume.provider_id = res.get("results")

            # 4. Delete temporary snapshot in simplyblock
            self.client.delete_snapshot(snapshot_provider_id)

            # 5. Apply QOS if needed
            self._setup_volume(volume)

            return {"provider_id": volume.provider_id}

        except Exception as e:
            LOG.error("Failed to clone volume: %s", e)
            raise SimplyblockDriverException(f"Volume clone failed: {e}")

    def remove_export(self, context, volume):
        pass

    def retype(self, context, volume, new_type, diff, host):
        # force apply qos from new volume type
        self._setup_volume(volume, new_type, raise_on_error=True, force_qos_update=True)
        return True, {}

    def _extract_backend_id(self, existing_ref):
        """Normalized lookup for backend identifier keys."""
        # Accept variety of keys commonly used by cinder manage payloads
        for key in ("source-id", "source_id", "id", "provider_id"):
            if key in existing_ref:
                return existing_ref.get(key)
        return None

    def manage_existing(self, volume, existing_ref):
        """Manage (import) an existing Simplyblock volume into Cinder.

        We expect existing_ref to include the backend volume id (source-id).
        This method will:x
          * Lookup the backend volume
          * Rename it to cinder naming convention: cinder-vol-<volume.name_id>
          * Attach attributes so we remember the original id/name/uuid
          * Set volume.provider_id so future operations target the correct object
        """
        LOG.info("Managing existing volume %s", existing_ref)
        sbid = self._extract_backend_id(existing_ref)
        if not sbid:
            raise exception.ManageExistingInvalidReference

        # 1) Get backend volume info
        try:
            sb_vol = self.client.get_volume(sbid)
        except SimplyblockAPIException as e:
            LOG.error("Failed to fetch Simplyblock volume %s: %s", sbid, e)
            raise exception.ManageExistingInvalidReference

        if not sb_vol:
            msg = _("Could not find Simplyblock volume %s") % sbid
            LOG.error(msg)
            raise exception.ManageExistingInvalidReference(msg)

        # 2) Prepare attributes to mark this as imported
        try:
            import_time = (
                volume.created_at.isoformat()
                if getattr(volume, "created_at", None)
                else None
            )
        except Exception:
            import_time = None

        attributes = {
            "uuid": volume.id,
            "is_clone": "False",
            "os_imported_at": import_time,
            "old_name": sb_vol.get("lvol_name"),
        }

        # 3) Rename/update backend volume so it follows cinder naming
        # and set attributes
        update_payload = {
            "name": f"cinder-vol-{volume.name_id}",
            "attributes": attributes,
        }

        try:
            self.client.update_volume(sbid, data=update_payload)
        except SimplyblockAPIException as e:
            LOG.error(
                "Failed to update Simplyblock volume %s "
                "during manage_existing: %s",
                sbid, e
            )
            raise

        # 4) Tell Cinder where the volume actually lives
        volume.provider_id = sbid

        LOG.info(
            "Successfully managed existing volume %s as cinder-vol-%s (provider_id=%s)",
            sb_vol.get("name"),
            volume.name_id,
            sbid,
        )

        return {"provider_id": sbid}

    def manage_existing_get_size(self, volume, existing_ref):
        """Return size (in GB) of an existing backend volume.

        Queries backend and returns an integer size in GB to let Cinder
        ensure requested size matches the real object.
        """
        LOG.info("Getting size of existing volume %s", existing_ref)
        sbid = self._extract_backend_id(existing_ref)
        if not sbid:
            raise exception.ManageExistingInvalidReference(
                _("manage_existing_get_size requires 'source-id' or equivalent")
            )

        try:
            sb_vol = self.client.get_volume(sbid)
        except SimplyblockAPIException as e:
            LOG.error("Failed to fetch Simplyblock volume %s: %s", sbid, e)
            raise exception.ManageExistingInvalidReference

        if not sb_vol:
            LOG.error("Could not find Simplyblock volume %s", volume)
            raise exception.ManageExistingInvalidReference

        # Try a few common fields returned by different APIs
        try:
            size_gb = int(math.ceil(int(sb_vol["size"]) / units.G))
        except:
            msg = _("Could not find Simplyblock volume %s")
            LOG.error("%s. Volume info: %s", msg, sb_vol)
            raise SimplyblockDriverException(msg)

        LOG.debug("manage_existing_get_size: backend %s size %d GB",
                  sbid, size_gb)
        return int(size_gb)

    def unmanage(self, volume):
        """Unmanage a Simplyblock volume previously imported into Cinder.

        Behaviour:
          - If provider_id is missing: nothing to do (log and return).
          - Otherwise: fetch backend volume, restore original name (if present
            in attributes['old_name']) and remove imported attributes we set
            during manage_existing (uuid, os_imported_at, is_clone, old_name).
        """
        vol_id = volume.provider_id
        if not vol_id:
            LOG.warning(
                "No provider_id for volume %s, skipping unmanage",
                getattr(volume, "name_id", volume.id)
            )
            return

        LOG.info(
            "Unmanaging volume %s (provider_id=%s)", volume.name_id, vol_id
        )

        try:
            sb_vol = self.client.get_volume(vol_id)
        except SimplyblockAPIException as e:
            LOG.error(
                "Failed to fetch Simplyblock volume %s for unmanage: %s",
                vol_id, e
            )
            raise SimplyblockDriverException(
                f"Failed to fetch backend volume: {e}"
            )

        if not sb_vol:
            LOG.error(
                "Could not find Simplyblock volume %s for unmanage", vol_id
            )
            raise SimplyblockDriverException(f"Backend volume {vol_id} not found")

        # Safely extract attributes (may be None)
        attrs = sb_vol.get("attributes") or {}
        old_name = attrs.get("old_name")

        # Build new attributes dict without imported keys
        keys_to_remove = {"uuid", "os_imported_at", "is_clone", "old_name"}
        new_attrs = {k: v for k, v in attrs.items() if k not in keys_to_remove}

        payload = {}
        if old_name:
            payload["name"] = old_name

        # If there are remaining attributes – send them; otherwise send empty dict to clear
        payload["attributes"] = new_attrs if new_attrs else {}

        try:
            self.client.update_volume(vol_id, data=payload)
        except SimplyblockAPIException as e:
            LOG.error("Failed to update Simplyblock volume %s during unmanage: %s", vol_id, e)
            raise exception.VolumeDriverException(message=f"Failed to unmanage backend volume: {e}")

        LOG.info("Successfully unmanaged Simplyblock volume %s (provider_id=%s)", volume.name_id, vol_id)



