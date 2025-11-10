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
# OpenStack Cinder Volume Driver for Simplyblock (NVMe/TCP and NVMe/RDMA)
import math

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units

from cinder.common import constants
from cinder import context
from cinder import exception
from cinder.i18n import _
from cinder import interface
from cinder.objects import fields
from cinder.volume import configuration
from cinder.volume import driver
from cinder.volume.drivers.simplyblock.client import SimplyblockAPIException
from cinder.volume.drivers.simplyblock.client import SimplyblockClient
from cinder.volume import qos_specs


LOG = logging.getLogger(__name__)

simplyblock_opts = [
    cfg.StrOpt("simplyblock_endpoint", help="Simplyblock API base URL"),
    cfg.StrOpt("simplyblock_cluster_secret",
               help="Simplyblock Cluster secret"),
    cfg.StrOpt("simplyblock_cluster_uuid", help="Cluster UUID"),
    cfg.StrOpt("simplyblock_pool_name", help="Simplyblock Pool Name"),
]
CONF = cfg.CONF
CONF.register_opts(simplyblock_opts, group=configuration.SHARED_CONF_GROUP)


class SimplyblockDriverException(exception.VolumeDriverException):
    message = _("Simplyblock Cinder driver failure: %(reason)s")


class SimplyblockRetryableException(exception.VolumeBackendAPIException):
    message = _("Retryable Simplyblock Exception encountered")


@interface.volumedriver
class SimplyblockDriver(driver.VolumeDriver):
    """OpenStack cinder driver to enable Simplyblock cluster

    via NVMe/TCP and NVMe/RDMA.

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

        self._storage_protocol = constants.NVMEOF
        self.configuration.append_config_values(simplyblock_opts)
        api_token = (f"{self.configuration.simplyblock_cluster_uuid} "
                     f"{self.configuration.simplyblock_cluster_secret}")
        LOG.debug("Initialising Simplyblock volume driver")
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
        data["total_capacity_gb"] = results["size_total"] / units.Gi
        data["thin_provisioning_support"] = False
        data["provisioned_capacity_gb"] = results["size_prov"] / units.Gi
        data["free_capacity_gb"] = results["size_free"] / units.Gi

        data["current_iops"] = (
            results["read_io_ps"] + results["write_io_ps"]
            + results["unmap_io_ps"]
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

    @staticmethod
    def _get_extra_specs(volume_type):
        """Get extra_specs of a volume_type.

        This fetches the keys from the volume type. Anything set
        from qos_specs will override keys set from extra_specs
        """
        if not volume_type:
            return None

        kvs = volume_type.get("extra_specs", {})

        extras = {}
        if 'simplyblock:fabric' in kvs:
            try:
                fabric = kvs['simplyblock:fabric'].lower()
                assert fabric in ['tcp', 'rdma']
                extras['fabric'] = fabric
            except Exception:
                LOG.warning("Invalid fabric value: %s",
                            kvs['simplyblock:fabric'])

            del kvs['simplyblock:fabric']

        if 'simplyblock:priority_class' in kvs:
            try:
                priority_class = int(kvs['simplyblock:priority_class'])
                assert 0 <= priority_class <= 7
                extras['lvol_priority_class'] = priority_class
            except Exception:
                LOG.warning("Invalid priority_class value: %s",
                            kvs['simplyblock:priority_class'])

            del kvs['simplyblock:priority_class']

        for key, value in kvs.items():
            if key.startswith('simplyblock:'):
                extras[key.replace('simplyblock:', '')] = value

        if extras == {}:
            return None

        LOG.debug("extra_specs in simplyblock format: %s", extras)
        return extras

    def create_volume(self, volume):
        """Create a new volume."""
        LOG.info("Creating volume %s of size %d GiB",
                 volume.name_id, volume.size)
        payload = {
            "name": f"cinder-vol-{volume.name_id}",
            "size_gib": volume.size,
        }
        qos = self._get_qos_settings(volume.volume_type)
        if qos:
            LOG.debug("Got QoS settings for volume %s: %s",
                      volume.name_id, qos)
            payload.update(qos)

        extra_specs = self._get_extra_specs(volume.volume_type)
        if extra_specs:
            LOG.debug("Got extra specs for volume %s: %s",
                      volume.name_id, extra_specs)
            payload.update(extra_specs)

        lv = self.client.create_volume(**payload)
        # Save volume ID in provider_id so we can find it later
        volume.provider_id = lv.get("results")
        LOG.info(
            "Volume %s created successfully. Provider id: %s",
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
        self, volume, volume_type=None, raise_on_error=False,
            force_qos_update=False
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
        LOG.info("Creating volume %s from snapshot %s",
                 volume.name_id, snapshot.id)

        # Simplyblock API does only allows to specify new_size
        # higher than the original one.
        # If the size should the same, do not specify it
        new_size_gib = volume.size if volume.size > snapshot.volume.size \
            else None
        res = self.client.clone_volume_from_snapshot(
            src_snapshot_id=snapshot.provider_id,
            name=f"cinder-vol-{volume.name_id}",
            new_size_gib=new_size_gib,
        )
        volume.provider_id = res.get("results")

        # apply qos
        self._setup_volume(volume)

        LOG.info(
            "Volume %s from snapshot %s created successfully. Provider id: %s",
            volume.name_id,
            snapshot.id,
            volume.provider_id,
        )
        return {"provider_id": volume.provider_id}

    def delete_volume(self, volume):
        """Delete a volume."""
        vol_id = volume.provider_id
        if not vol_id:
            LOG.warning("No provider_id for volume %s, skipping delete",
                        volume.name_id)
            return
        LOG.info("Deleting volume %s", vol_id)
        try:
            self.client.delete_volume(vol_id)
        except Exception as e:
            LOG.error("Failed to delete volume %s: %s", vol_id, e)

    def extend_volume(self, volume, new_size):
        """Extend a volume to new_size in GiB."""
        vol_id = volume.provider_id
        if not vol_id:
            raise SimplyblockDriverException(
                "Missing provider_id for volume extend"
            )
        LOG.info("Extending volume %s to size %d GiB", vol_id, new_size)
        new_size_str = f"{new_size}G"
        self.client.extend_volume(vol_id, new_size_str)

    def initialize_connection(self, volume, connector, **kwargs):
        hostnqn = connector.get("nqn")
        found_dsc = connector.get("found_dsc")
        host_ips = connector.get("host_ips", [])
        LOG.info("Current host hostNQN is %s and IP(s) are %s",
                 hostnqn, host_ips)
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
            (p["ip"], p["port"], p["transport"])
            for p in response["results"]
        ]

        # multiple portals on the same controller
        # are not supported in os-bricks 2025.1
        # provide only primary connection to avoid errors
        # portals = portals[:1]
        target_nqn = response["results"][-1]["nqn"]
        props = {
            "target_nqn": target_nqn,
            "portals": portals,
            "host_nqn": hostnqn,
            "volume_id": volume["id"],
            "access_mode": "rw",
            "discard": False,
            "vol_uuid": volume.provider_id,
        }
        return {"driver_volume_type": "nvmeof", "data": props}

    def terminate_connection(self, volume, connector, **kwargs):
        """Remove access to a volume.

        Since we don't create any resources in initialize_connection,
        there's nothing to clean up here.
        Simplyblock manages connections automatically.
        """
        LOG.info("Terminate connection for volume %s (connector: %s). "
                 "No action required for Simplyblock driver.",
                 volume.id, connector)
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
        LOG.info("Creating snapshot %s from volume %s",
                 snapshot.id, snapshot.volume_id)
        res = self.client.create_snapshot(
            volume_id=snapshot.volume.provider_id,
            name=f"cinder-snap-{snapshot.id}"
        )
        snapshot.provider_id = res.get("results")
        LOG.info(
            "Snapshot %s from volume %s created successfully. Provider id: %s",
            snapshot.id, snapshot.volume_id, snapshot.provider_id
        )
        return {"provider_id": snapshot.provider_id}

    def delete_snapshot(self, snapshot):
        """Delete a snapshot."""
        LOG.info("Deleting snapshot %s. Provider id: snapshot.provider_id",
                 snapshot.id)
        self.client.delete_snapshot(snapshot.provider_id)

    # ---------------- Housekeeping ----------------
    def initialize_connection_snapshot(self, snapshot, connector, **kwargs):
        """Not implemented: snapshots cannot be directly attached."""
        raise NotImplementedError()

    def terminate_connection_snapshot(self, snapshot, connector, **kwargs):
        """Not implemented: snapshots cannot be directly detached."""
        raise NotImplementedError()

    def create_cloned_volume(self, volume, src_vref):
        """Create a clone of the specified volume."""
        LOG.info(
            "Creating clone of volume %s (provider_id %s) as a new volume %s",
            src_vref.name_id,
            src_vref.provider_id,
            volume.name_id
        )
        try:
            # 1. Create temporary snapshot from source volume
            res = self.client.create_snapshot(
                volume_id=src_vref.provider_id,
                name=f"cinder-tmp-snap-vol-"
                     f"{src_vref.name_id}-{volume.name_id}",
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
        extra_specs_old = self._get_extra_specs(volume.volume_type)
        extra_specs_new = self._get_extra_specs(new_type)
        if extra_specs_old != extra_specs_new:
            LOG.info(
                "Retype not possible: extra_specs differ. Old: %s, New: %s",
                extra_specs_old, extra_specs_new
            )
            return False, {}

        # force apply qos from new volume type
        self._setup_volume(
            volume, new_type, raise_on_error=True, force_qos_update=True
        )
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
          * Set volume.provider_id so future operations target
            the correct object
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
            "Successfully managed existing volume %s as cinder-vol-%s "
            "(provider_id=%s)",
            sb_vol.get("name"),
            volume.name_id,
            sbid,
        )

        return {"provider_id": sbid}

    def manage_existing_get_size(self, volume, existing_ref):
        """Return size (in GiB) of an existing backend volume.

        Queries backend and returns an integer size in GiB to let Cinder
        ensure requested size matches the real object.
        """
        LOG.info("Getting size of existing volume %s", existing_ref)
        sbid = self._extract_backend_id(existing_ref)
        if not sbid:
            raise exception.ManageExistingInvalidReference(
                _("manage_existing_get_size requires 'source-id' "
                  "or equivalent")
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
            size_gib = int(math.ceil(int(sb_vol["size"]) / units.Gi))
        except Exception:
            msg = _("Could not find Simplyblock volume %s")
            LOG.error("%s. Volume info: %s", msg, sb_vol)
            raise SimplyblockDriverException(msg)

        LOG.debug("manage_existing_get_size: backend %s size %d GiB",
                  sbid, size_gib)
        return int(size_gib)

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
            raise SimplyblockDriverException(
                f"Backend volume {vol_id} not found"
            )

        # Safely extract attributes (may be None)
        attrs = sb_vol.get("attributes") or {}
        old_name = attrs.get("old_name")

        # Build new attributes dict without imported keys
        keys_to_remove = {"uuid", "os_imported_at", "is_clone", "old_name"}
        new_attrs = {k: v for k, v in attrs.items() if k not in keys_to_remove}

        payload = {}
        if old_name:
            payload["name"] = old_name

        # If there are remaining attributes – send them;
        # otherwise send empty dict to clear
        payload["attributes"] = new_attrs if new_attrs else {}

        try:
            self.client.update_volume(vol_id, data=payload)
        except SimplyblockAPIException as e:
            LOG.error(
                "Failed to update Simplyblock volume %s during unmanage: %s",
                vol_id, e
            )
            raise exception.VolumeDriverException(
                message=f"Failed to unmanage backend volume: {e}")

        LOG.info(
            "Successfully unmanaged Simplyblock volume %s (provider_id=%s)",
            volume.name_id, vol_id
        )

    def migrate_volume(self, ctxt, volume, host):
        """Migrate volume between hosts using the same Simplyblock cluster.

        If the target host uses the same Simplyblock cluster UUID and pool ,
        we reuse the existing provider_id (no backend copy required).
        Otherwise, migration is not handled here.
        """
        LOG.info("Migrate volume %(vol_id)s to %(host)s.",
                 {"vol_id": volume.id, "host": host["host"]})

        if (volume.status != fields.VolumeStatus.AVAILABLE and
                volume.status != fields.VolumeStatus.RETYPING):
            msg = _("Volume status must be 'available' or 'retyping' to "
                    "execute storage assisted migration.")
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        capabilities = host.get("capabilities", {}) or {}
        LOG.debug("New host capabilities %s", capabilities)
        dest_cluster_uuid = capabilities.get("simplyblock_cluster_uuid")
        dest_pool_name = capabilities.get("simplyblock_pool_name")

        src_cluster_uuid = self.configuration.simplyblock_cluster_uuid
        src_pool_name = self.configuration.simplyblock_pool_name

        same_cluster = (
            dest_cluster_uuid
            and dest_cluster_uuid == src_cluster_uuid
            and dest_pool_name
            and dest_pool_name == src_pool_name
        )

        if same_cluster:
            LOG.info(
                "Migration within the same Simplyblock cluster (%s) and pool "
                "(%s). Reusing provider_id=%s.",
                dest_cluster_uuid,
                dest_pool_name,
                volume.provider_id,
            )
            return True, {"provider_id": volume.provider_id}

        LOG.warning(
            "Migration target host %s uses a different Simplyblock cluster "
            "or pool (cluster: %s != %s, pool: %s != %s). "
            "Storage assisted migration not supported.",
            host.get("host"),
            dest_cluster_uuid,
            src_cluster_uuid,
            dest_pool_name,
            src_pool_name,
        )
        return False, None
