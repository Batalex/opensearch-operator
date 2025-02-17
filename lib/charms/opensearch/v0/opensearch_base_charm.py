# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Base class for the OpenSearch Operators."""
import logging
import random
from abc import abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Type

from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.opensearch.v0.constants_charm import (
    AdminUserInitProgress,
    CertsExpirationError,
    ClientRelationName,
    ClusterHealthRed,
    ClusterHealthUnknown,
    COSPort,
    COSRelationName,
    COSRole,
    COSUser,
    PeerRelationName,
    PluginConfigChangeError,
    PluginConfigStart,
    RequestUnitServiceOps,
    SecurityIndexInitProgress,
    ServiceIsStopping,
    ServiceStartError,
    ServiceStopped,
    TLSNewCertsRequested,
    TLSNotFullyConfigured,
    TLSRelationBrokenError,
    WaitingToStart,
)
from charms.opensearch.v0.constants_secrets import ADMIN_PW, ADMIN_PW_HASH
from charms.opensearch.v0.constants_tls import TLS_RELATION, CertType
from charms.opensearch.v0.helper_charm import DeferTriggerEvent, Status
from charms.opensearch.v0.helper_cluster import ClusterTopology, Node
from charms.opensearch.v0.helper_networking import (
    get_host_ip,
    is_reachable,
    reachable_hosts,
    unit_ip,
    units_ips,
)
from charms.opensearch.v0.helper_security import (
    cert_expiration_remaining_hours,
    generate_hashed_password,
    generate_password,
)
from charms.opensearch.v0.opensearch_backups import OpenSearchBackup
from charms.opensearch.v0.opensearch_config import OpenSearchConfig
from charms.opensearch.v0.opensearch_distro import OpenSearchDistribution
from charms.opensearch.v0.opensearch_exceptions import (
    OpenSearchError,
    OpenSearchHAError,
    OpenSearchHttpError,
    OpenSearchNotFullyReadyError,
    OpenSearchStartError,
    OpenSearchStartTimeoutError,
    OpenSearchStopError,
)
from charms.opensearch.v0.opensearch_fixes import OpenSearchFixes
from charms.opensearch.v0.opensearch_health import HealthColors, OpenSearchHealth
from charms.opensearch.v0.opensearch_internal_data import RelationDataStore, Scope
from charms.opensearch.v0.opensearch_locking import OpenSearchOpsLock
from charms.opensearch.v0.opensearch_nodes_exclusions import (
    ALLOCS_TO_DELETE,
    VOTING_TO_DELETE,
    OpenSearchExclusions,
)
from charms.opensearch.v0.opensearch_peer_clusters import (
    OpenSearchPeerClustersManager,
    OpenSearchProvidedRolesException,
    StartMode,
)
from charms.opensearch.v0.opensearch_plugin_manager import OpenSearchPluginManager
from charms.opensearch.v0.opensearch_plugins import OpenSearchPluginError
from charms.opensearch.v0.opensearch_relation_provider import OpenSearchProvider
from charms.opensearch.v0.opensearch_secrets import OpenSearchSecrets
from charms.opensearch.v0.opensearch_tls import OpenSearchTLS
from charms.opensearch.v0.opensearch_users import OpenSearchUserManager
from charms.rolling_ops.v0.rollingops import RollingOpsManager
from charms.tls_certificates_interface.v3.tls_certificates import (
    CertificateAvailableEvent,
)
from ops.charm import (
    ActionEvent,
    CharmBase,
    ConfigChangedEvent,
    LeaderElectedEvent,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationCreatedEvent,
    RelationDepartedEvent,
    RelationJoinedEvent,
    StartEvent,
    StorageDetachingEvent,
    UpdateStatusEvent,
)
from ops.framework import EventBase, EventSource
from ops.model import BlockedStatus, MaintenanceStatus, WaitingStatus

# The unique Charmhub library identifier, never change it
LIBID = "cba015bae34642baa1b6bb27bb35a2f7"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 2


SERVICE_MANAGER = "service"
STORAGE_NAME = "opensearch-data"


logger = logging.getLogger(__name__)


class OpenSearchBaseCharm(CharmBase):
    """Base class for OpenSearch charms."""

    defer_trigger_event = EventSource(DeferTriggerEvent)

    def __init__(self, *args, distro: Type[OpenSearchDistribution] = None):
        super().__init__(*args)

        if distro is None:
            raise ValueError("The type of the opensearch distro must be specified.")

        self.opensearch = distro(self, PeerRelationName)
        self.opensearch_peer_cm = OpenSearchPeerClustersManager(self)
        self.opensearch_config = OpenSearchConfig(self.opensearch)
        self.opensearch_exclusions = OpenSearchExclusions(self)
        self.opensearch_fixes = OpenSearchFixes(self)
        self.peers_data = RelationDataStore(self, PeerRelationName)
        self.secrets = OpenSearchSecrets(self, PeerRelationName)
        self.tls = OpenSearchTLS(self, TLS_RELATION)
        self.status = Status(self)
        self.health = OpenSearchHealth(self)
        self.ops_lock = OpenSearchOpsLock(self)
        self.cos_integration = COSAgentProvider(
            self,
            relation_name=COSRelationName,
            metrics_endpoints=[],
            scrape_configs=self._scrape_config,
            refresh_events=[self.on.set_password_action, self.on.secret_changed],
            metrics_rules_dir="./src/alert_rules/prometheus",
            log_slots=["opensearch:logs"],
        )

        self.plugin_manager = OpenSearchPluginManager(self)
        self.backup = OpenSearchBackup(self)

        self.service_manager = RollingOpsManager(
            self, relation=SERVICE_MANAGER, callback=self._start_opensearch
        )
        self.user_manager = OpenSearchUserManager(self)
        self.opensearch_provider = OpenSearchProvider(self)

        # helper to defer events without any additional logic
        self.framework.observe(self.defer_trigger_event, self._on_defer_trigger)

        self.framework.observe(self.on.leader_elected, self._on_leader_elected)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        self.framework.observe(
            self.on[PeerRelationName].relation_created, self._on_peer_relation_created
        )
        self.framework.observe(
            self.on[PeerRelationName].relation_joined, self._on_peer_relation_joined
        )
        self.framework.observe(
            self.on[PeerRelationName].relation_changed, self._on_peer_relation_changed
        )
        self.framework.observe(
            self.on[PeerRelationName].relation_departed, self._on_peer_relation_departed
        )
        self.framework.observe(
            self.on[STORAGE_NAME].storage_detaching, self._on_opensearch_data_storage_detaching
        )

        self.framework.observe(self.on.set_password_action, self._on_set_password_action)
        self.framework.observe(self.on.get_password_action, self._on_get_password_action)

    def _on_defer_trigger(self, _: DeferTriggerEvent):
        """Hook for the trigger_defer event."""
        pass

    def _on_leader_elected(self, event: LeaderElectedEvent):
        """Handle leader election event."""
        if self.peers_data.get(Scope.APP, "security_index_initialised", False):
            # Leader election event happening after a previous leader got killed
            if not self.opensearch.is_node_up():
                event.defer()
                return

            if self.health.apply() in [HealthColors.UNKNOWN, HealthColors.YELLOW_TEMP]:
                event.defer()

            self._compute_and_broadcast_updated_topology(self._get_nodes(True))
            return

        # TODO: check if cluster can start independently

        if not self.peers_data.get(Scope.APP, "admin_user_initialized"):
            self.status.set(MaintenanceStatus(AdminUserInitProgress))

        # User config is currently in a default state, which contains multiple insecure default
        # users. Purge the user list before initialising the users the charm requires.
        self._purge_users()

        # this is in case we're coming from 0 to N units, we don't want to use the rest api
        self._put_admin_user()

        self.status.clear(AdminUserInitProgress)

    def _on_start(self, event: StartEvent):
        """Triggered when on start. Set the right node role."""
        if self.opensearch.is_node_up():
            if self.peers_data.get(Scope.APP, "security_index_initialised"):
                # in the case where it was on WaitingToStart status, event got deferred
                # and the service started in between, put status back to active
                self.status.clear(WaitingToStart)

            # cleanup bootstrap conf in the node if existing
            if self.peers_data.get(Scope.UNIT, "bootstrap_contributor"):
                self._cleanup_bootstrap_conf_if_applies()

            return

        if not self._is_tls_fully_configured():
            self.status.set(BlockedStatus(TLSNotFullyConfigured))
            event.defer()
            return

        self.status.clear(TLSNotFullyConfigured)

        # apply the directives computed and emitted by the peer cluster manager
        if not self._apply_peer_cm_directives_and_start():
            event.defer()
            return

        # configure clients auth
        self.opensearch_config.set_client_auth()

        # request the start of OpenSearch
        self.status.set(WaitingStatus(RequestUnitServiceOps.format("start")))
        self.on[self.service_manager.name].acquire_lock.emit(callback_override="_start_opensearch")

    def _apply_peer_cm_directives_and_start(self) -> bool:
        """Apply the directives computed by the opensearch peer cluster manager."""
        if not (deployment_desc := self.opensearch_peer_cm.deployment_desc()):
            # the deployment description hasn't finished being computed by the leader
            return False

        # check possibility to start
        if self.opensearch_peer_cm.can_start(deployment_desc):
            try:
                nodes = self._get_nodes(False)
                self.opensearch_peer_cm.validate_roles(nodes, on_new_unit=True)
            except OpenSearchHttpError:
                return False
            except OpenSearchProvidedRolesException as e:
                self.unit.status = BlockedStatus(str(e))
                return False

            # request the start of OpenSearch
            self.status.set(WaitingStatus(RequestUnitServiceOps.format("start")))
            self.on[self.service_manager.name].acquire_lock.emit(
                callback_override="_start_opensearch"
            )
            return True

        if self.unit.is_leader():
            self.opensearch_peer_cm.apply_status_if_needed(deployment_desc)

        return False

    def _on_peer_relation_created(self, event: RelationCreatedEvent):
        """Event received by the new node joining the cluster."""
        current_secrets = self.secrets.get_object(Scope.APP, CertType.APP_ADMIN.val)

        # In the case of the first units before TLS is initialized
        if not current_secrets:
            if not self.unit.is_leader():
                event.defer()
            return

        # in the case the cluster was bootstrapped with multiple units at the same time
        # and the certificates have not been generated yet
        if not current_secrets.get("cert") or not current_secrets.get("chain"):
            event.defer()
            return

        # Store the "Admin" certificate, key and CA on the disk of the new unit
        self.store_tls_resources(CertType.APP_ADMIN, current_secrets, override_admin=False)

    def _on_peer_relation_joined(self, event: RelationJoinedEvent):
        """Event received by all units when a new node joins the cluster."""
        if not self.unit.is_leader():
            return

        if (
            not self.peers_data.get(Scope.APP, "security_index_initialised")
            or not self.opensearch.is_node_up()
        ):
            return

        new_unit_host = unit_ip(self, event.unit, PeerRelationName)
        if not is_reachable(new_unit_host, self.opensearch.port):
            event.defer()
            return

        try:
            nodes = self._get_nodes(True)
        except OpenSearchHttpError:
            event.defer()
            return

        # we want to re-calculate the topology only once when latest unit joins
        if len(nodes) == self.app.planned_units():
            self._compute_and_broadcast_updated_topology(nodes)
        else:
            event.defer()

    def _on_peer_relation_changed(self, event: RelationChangedEvent):
        """Handle peer relation changes."""
        if (
            self.unit.is_leader()
            and self.opensearch.is_node_up()
            and self.health.apply() in [HealthColors.UNKNOWN, HealthColors.YELLOW_TEMP]
        ):
            # we defer because we want the temporary status to be updated
            event.defer()
            self.defer_trigger_event.emit()

        for relation in self.model.relations.get(ClientRelationName, []):
            self.opensearch_provider.update_endpoints(relation)

        # register new cm addresses on every node
        self._add_cm_addresses_to_conf()

        app_data = event.relation.data.get(event.app)
        if self.unit.is_leader():
            # Recompute the node roles in case self-healing didn't trigger leader related event
            self._recompute_roles_if_needed(event)
        elif app_data:
            # if app_data + app_data["nodes_config"]: Reconfigure + restart node on the unit
            self._reconfigure_and_restart_unit_if_needed()

        unit_data = event.relation.data.get(event.unit)
        if not unit_data:
            return

        if unit_data.get(VOTING_TO_DELETE) or unit_data.get(ALLOCS_TO_DELETE):
            self.opensearch_exclusions.cleanup()

        if self.unit.is_leader() and unit_data.get("bootstrap_contributor"):
            contributor_count = self.peers_data.get(Scope.APP, "bootstrap_contributors_count", 0)
            self.peers_data.put(Scope.APP, "bootstrap_contributors_count", contributor_count + 1)

    def _on_peer_relation_departed(self, event: RelationDepartedEvent):
        """Relation departed event."""
        if not (self.unit.is_leader() and self.opensearch.is_node_up()):
            return

        remaining_nodes = [
            node
            for node in self._get_nodes(True)
            if node.name != event.departing_unit.name.replace("/", "-")
        ]

        if len(remaining_nodes) == self.app.planned_units():
            self._compute_and_broadcast_updated_topology(remaining_nodes)
        else:
            event.defer()

    def _on_opensearch_data_storage_detaching(self, _: StorageDetachingEvent):  # noqa: C901
        """Triggered when removing unit, Prior to the storage being detached."""
        # acquire lock to ensure only 1 unit removed at a time
        self.ops_lock.acquire()

        # if the leader is departing, and this hook fails "leader elected" won"t trigger,
        # so we want to re-balance the node roles from here
        if self.unit.is_leader():
            if self.app.planned_units() > 1 and (self.opensearch.is_node_up() or self.alt_hosts):
                remaining_nodes = [
                    node
                    for node in self._get_nodes(self.opensearch.is_node_up())
                    if node.name != self.unit_name
                ]
                self._compute_and_broadcast_updated_topology(remaining_nodes)
            elif self.app.planned_units() == 0:
                self.peers_data.delete(Scope.APP, "bootstrap_contributors_count")
                self.peers_data.delete(Scope.APP, "nodes_config")

                # todo: remove this if snap storage reuse is solved.
                self.peers_data.delete(Scope.APP, "security_index_initialised")

        # we attempt to flush the translog to disk
        if self.opensearch.is_node_up():
            try:
                self.opensearch.request("POST", "/_flush?wait_for_ongoing")
            except OpenSearchHttpError:
                # if it's a failed attempt we move on
                pass
        try:
            self._stop_opensearch()

            # safeguards in case planned_units > 0
            if self.app.planned_units() > 0:
                # check cluster status
                if self.alt_hosts:
                    health_color = self.health.apply(
                        wait_for_green_first=True, use_localhost=False
                    )
                    if health_color == HealthColors.RED:
                        raise OpenSearchHAError(ClusterHealthRed)
                else:
                    raise OpenSearchHAError(ClusterHealthUnknown)
        finally:
            # release lock
            self.ops_lock.release()

    def _on_update_status(self, event: UpdateStatusEvent):
        """On update status event.

        We want to periodically check for the following:
        1- Do we have users that need to be deleted, and if so we need to delete them.
        2- The system requirements are still met
        3- every 6 hours check if certs are expiring soon (in 7 days),
            as a safeguard in case relation broken. As there will be data loss
            without the user noticing in case the cert of the unit transport layer expires.
            So we want to stop opensearch in that case, since it cannot be recovered from.
        """
        # if there are missing system requirements defer
        missing_sys_reqs = self.opensearch.missing_sys_requirements()
        if len(missing_sys_reqs) > 0:
            self.status.set(BlockedStatus(" - ".join(missing_sys_reqs)))
            return

        # if node already shutdown - leave
        if not self.opensearch.is_node_up():
            return

        # if there are exclusions to be removed
        if self.unit.is_leader():
            self.opensearch_exclusions.cleanup()

            health = self.health.apply()
            if health != HealthColors.GREEN:
                event.defer()

            if health == HealthColors.UNKNOWN:
                return

        for relation in self.model.relations.get(ClientRelationName, []):
            self.opensearch_provider.update_endpoints(relation)

        self.user_manager.remove_users_and_roles()

        # If relation not broken - leave
        if self.model.get_relation("certificates") is not None:
            return

        # handle when/if certificates are expired
        self._check_certs_expiration(event)

    def _on_config_changed(self, event: ConfigChangedEvent):
        """On config changed event. Useful for IP changes or for user provided config changes."""
        if self.opensearch_config.update_host_if_needed():
            self.status.set(MaintenanceStatus(TLSNewCertsRequested))
            self._delete_stored_tls_resources()
            self.tls.request_new_unit_certificates()

            # since when an IP change happens, "_on_peer_relation_joined" won't be called,
            # we need to alert the leader that it must recompute the node roles for any unit whose
            # roles were changed while the current unit was cut-off from the rest of the network
            self.on[PeerRelationName].relation_joined.emit(
                self.model.get_relation(PeerRelationName)
            )

        if self.unit.is_leader():
            # run peer cluster manager processing
            self.opensearch_peer_cm.run()
        elif not self.opensearch_peer_cm.deployment_desc():
            # deployment desc not initialized yet by leader
            event.defer()
            return

        self.status.set(MaintenanceStatus(PluginConfigStart))
        try:
            if self.plugin_manager.run():
                self.on[self.service_manager.name].acquire_lock.emit(
                    callback_override="_restart_opensearch"
                )
        except OpenSearchNotFullyReadyError:
            logger.warning("Plugin management: cluster not ready yet at config changed")
            event.defer()
            return
        except OpenSearchPluginError:
            self.status.set(BlockedStatus(PluginConfigChangeError))
            event.defer()
            return
        self.status.clear(PluginConfigChangeError)
        self.status.clear(PluginConfigStart)

    def _on_set_password_action(self, event: ActionEvent):
        """Set new admin password from user input or generate if not passed."""
        if not self.unit.is_leader():
            event.fail("The action can be run only on leader unit.")
            return

        user_name = event.params.get("username")
        if user_name not in ["admin", COSUser]:
            event.fail(f"Only the 'admin' and {COSUser} username is allowed for this action.")
            return

        password = event.params.get("password") or generate_password()
        try:
            label = self.secrets.password_key(user_name)
            self._put_admin_user(password)
            password = self.secrets.get(Scope.APP, label)
            event.set_results({label: password})
        except OpenSearchError as e:
            event.fail(f"Failed changing the password: {e}")

    def _on_get_password_action(self, event: ActionEvent):
        """Return the password and cert chain for the admin user of the cluster."""
        user_name = event.params.get("username")
        if user_name not in ["admin", COSUser]:
            event.fail(f"Only the 'admin' and {COSUser} username is allowed for this action.")
            return

        if not self._is_tls_fully_configured():
            event.fail(f"{user_name} user or TLS certificates not configured yet.")
            return

        password = self.secrets.get(Scope.APP, self.secrets.password_key(user_name))
        cert = self.secrets.get_object(
            Scope.APP, CertType.APP_ADMIN.val
        )  # replace later with new user certs

        event.set_results(
            {
                "username": user_name,
                "password": password,
                "ca-chain": cert["chain"],
            }
        )

    def on_tls_conf_set(
        self, _: CertificateAvailableEvent, scope: Scope, cert_type: CertType, renewal: bool
    ):
        """Called after certificate ready and stored on the corresponding scope databag.

        - Store the cert on the file system, on all nodes for APP certificates
        - Update the corresponding yaml conf files
        - Run the security admin script
        """
        # Get the list of stored secrets for this cert
        current_secrets = self.secrets.get_object(scope, cert_type.val)

        # Store cert/key on disk - must happen after opensearch stop for transport certs renewal
        self.store_tls_resources(cert_type, current_secrets)

        if scope == Scope.UNIT:
            # node http or transport cert
            self.opensearch_config.set_node_tls_conf(cert_type, current_secrets)
        else:
            # write the admin cert conf on all units, in case there is a leader loss + cert renewal
            self.opensearch_config.set_admin_tls_conf(current_secrets)

        # In case of renewal of the unit transport layer cert - restart opensearch
        if renewal and self._is_tls_fully_configured():
            self.on[self.service_manager.name].acquire_lock.emit(
                callback_override="_restart_opensearch"
            )

    def on_tls_relation_broken(self, _: RelationBrokenEvent):
        """As long as all certificates are produced, we don't do anything."""
        if self._is_tls_fully_configured():
            return

        # Otherwise, we block.
        self.status.set(BlockedStatus(TLSRelationBrokenError))

    def _is_tls_fully_configured(self) -> bool:
        """Check if TLS fully configured meaning the admin user configured & 3 certs present."""
        # In case the initialisation of the admin user is not finished yet
        if not self.peers_data.get(Scope.APP, "admin_user_initialized"):
            return False

        admin_secrets = self.secrets.get_object(Scope.APP, CertType.APP_ADMIN.val)
        if not admin_secrets or not admin_secrets.get("cert") or not admin_secrets.get("chain"):
            return False

        unit_transport_secrets = self.secrets.get_object(Scope.UNIT, CertType.UNIT_TRANSPORT.val)
        if not unit_transport_secrets or not unit_transport_secrets.get("cert"):
            return False

        unit_http_secrets = self.secrets.get_object(Scope.UNIT, CertType.UNIT_HTTP.val)
        if not unit_http_secrets or not unit_http_secrets.get("cert"):
            return False

        return self._are_all_tls_resources_stored()

    def _start_opensearch(self, event: EventBase) -> None:  # noqa: C901
        """Start OpenSearch, with a generated or passed conf, if all resources configured."""
        if self.opensearch.is_started():
            try:
                self._post_start_init()
            except (OpenSearchHttpError, OpenSearchNotFullyReadyError):
                event.defer()
                self.defer_trigger_event.emit()
            return

        if not self._can_service_start():
            self.peers_data.delete(Scope.UNIT, "starting")
            event.defer()

            # emit defer trigger event which won't do anything to force retry of current event
            self.defer_trigger_event.emit()
            return

        if self.peers_data.get(Scope.UNIT, "starting", False) and self.opensearch.is_failed():
            self.peers_data.delete(Scope.UNIT, "starting")
            event.defer()
            return

        self.unit.status = WaitingStatus(WaitingToStart)

        rel = self.model.get_relation(PeerRelationName)
        for unit in rel.units.union({self.unit}):
            if rel.data[unit].get("starting") == "True":
                event.defer()
                return

        self.peers_data.put(Scope.UNIT, "starting", True)

        try:
            # Retrieve the nodes of the cluster, needed to configure this node
            nodes = self._get_nodes(False)

            # validate the roles prior to starting
            self.opensearch_peer_cm.validate_roles(nodes, on_new_unit=True)

            # Set the configuration of the node
            self._set_node_conf(nodes)
        except OpenSearchHttpError:
            self.peers_data.delete(Scope.UNIT, "starting")
            event.defer()
            self._post_start_init()
            return
        except OpenSearchProvidedRolesException as e:
            logger.exception(e)
            self.peers_data.delete(Scope.UNIT, "starting")
            event.defer()
            self.unit.status = BlockedStatus(str(e))
            return

        try:
            self.opensearch.start(
                wait_until_http_200=(
                    not self.unit.is_leader()
                    or self.peers_data.get(Scope.APP, "security_index_initialised", False)
                )
            )
            self._post_start_init()
        except (OpenSearchStartTimeoutError, OpenSearchNotFullyReadyError):
            event.defer()
            # emit defer_trigger event which won't do anything to force retry of current event
            self.defer_trigger_event.emit()
        except OpenSearchStartError as e:
            logger.exception(e)
            self.peers_data.delete(Scope.UNIT, "starting")
            self.status.set(BlockedStatus(ServiceStartError))
            event.defer()
            self.defer_trigger_event.emit()

    def _post_start_init(self):
        """Initialization post OpenSearch start."""
        # initialize the security index if needed (and certs written on disk etc.)
        if self.unit.is_leader() and not self.peers_data.get(
            Scope.APP, "security_index_initialised"
        ):
            admin_secrets = self.secrets.get_object(Scope.APP, CertType.APP_ADMIN.val)
            self._initialize_security_index(admin_secrets)
            self.peers_data.put(Scope.APP, "security_index_initialised", True)

        # it sometimes takes a few seconds before the node is fully "up" otherwise a 503 error
        # may be thrown when calling a node - we want to ensure this node is perfectly ready
        # before marking it as ready
        if not self.opensearch.is_node_up():
            raise OpenSearchNotFullyReadyError("Node started but not full ready yet.")

        # cleanup bootstrap conf in the node
        if self.peers_data.get(Scope.UNIT, "bootstrap_contributor"):
            self._cleanup_bootstrap_conf_if_applies()

        # Remove the exclusions that could not be removed when no units were online
        self.opensearch_exclusions.delete_current()

        # Remove the 'starting' flag on the unit
        self.peers_data.delete(Scope.UNIT, "starting")

        # apply post_start fixes to resolve start related upstream bugs
        self.opensearch_fixes.apply_on_start()

        # apply cluster health
        self.health.apply()

        # Creating the monitoring user
        self._put_monitoring_user()

        # clear waiting to start status
        self.status.clear(WaitingToStart)

    def _stop_opensearch(self) -> None:
        """Stop OpenSearch if possible."""
        self.status.set(WaitingStatus(ServiceIsStopping))

        # 1. Add current node to the voting + alloc exclusions
        self.opensearch_exclusions.add_current()

        # 2. stop the service
        self.opensearch.stop()
        self.status.set(WaitingStatus(ServiceStopped))

        # 3. Remove the exclusions
        self.opensearch_exclusions.delete_current()

    def _restart_opensearch(self, event: EventBase) -> None:
        """Restart OpenSearch if possible."""
        if not self.peers_data.get(Scope.UNIT, "starting", False):
            try:
                self._stop_opensearch()
            except OpenSearchStopError as e:
                logger.exception(e)
                event.defer()
                self.status.set(WaitingStatus(ServiceIsStopping))
                return

        self._start_opensearch(event)

    def _can_service_start(self) -> bool:
        """Return if the opensearch service can start."""
        # if there are any missing system requirements leave
        missing_sys_reqs = self.opensearch.missing_sys_requirements()
        if len(missing_sys_reqs) > 0:
            self.status.set(BlockedStatus(" - ".join(missing_sys_reqs)))
            return False

        if self.unit.is_leader():
            return True

        if not self.peers_data.get(Scope.APP, "security_index_initialised", False):
            return False

        if not self.alt_hosts:
            return False

        # When a new unit joins, replica shards are automatically added to it. In order to prevent
        # overloading the cluster, units must be started one at a time. So we defer starting
        # opensearch until all shards in other units are in a "started" or "unassigned" state.
        try:
            if self.health.apply(use_localhost=False, app=False) == HealthColors.YELLOW_TEMP:
                return False
        except OpenSearchHttpError:
            # this means that the leader unit is not reachable (not started yet),
            # meaning it's a new cluster, so we can safely start the OpenSearch service
            pass

        return True

    def _purge_users(self):
        """Removes all users from internal_users yaml config.

        This is to be used when starting up the charm, to remove unnecessary default users.
        """
        try:
            internal_users = self.opensearch.config.load(
                "opensearch-security/internal_users.yml"
            ).keys()
        except FileNotFoundError:
            # internal_users.yml hasn't been initialised yet, so skip purging for now.
            return

        for user in internal_users:
            if user != "_meta":
                self.opensearch.config.delete("opensearch-security/internal_users.yml", user)

    def _put_admin_user(self, pwd: Optional[str] = None):
        """Change password of Admin user."""
        # update
        if pwd is not None:
            hashed_pwd, pwd = generate_hashed_password(pwd)
            resp = self.opensearch.request(
                "PATCH",
                "/_plugins/_security/api/internalusers/admin",
                [{"op": "replace", "path": "/hash", "value": hashed_pwd}],
            )
            if resp.get("status") != "OK":
                raise OpenSearchError(f"{resp}")
        else:
            hashed_pwd = self.secrets.get(Scope.APP, ADMIN_PW_HASH)
            if not hashed_pwd:
                hashed_pwd, pwd = generate_hashed_password()

            # reserved: False, prevents this resource from being update-protected from:
            # updates made on the dashboard or the rest api.
            # we grant the admin user all opensearch access + security_rest_api_access
            self.opensearch.config.put(
                "opensearch-security/internal_users.yml",
                "admin",
                {
                    "hash": hashed_pwd,
                    "reserved": False,
                    "backend_roles": ["admin"],
                    "opendistro_security_roles": [
                        "security_rest_api_access",
                        "all_access",
                    ],
                    "description": "Admin user",
                },
            )

        self.secrets.put(Scope.APP, ADMIN_PW, pwd)
        self.secrets.put(Scope.APP, ADMIN_PW_HASH, hashed_pwd)
        self.peers_data.put(Scope.APP, "admin_user_initialized", True)

    def _put_monitoring_user(self):
        """Create the monitoring user, with the right security role."""
        users = self.user_manager.get_users()

        if users and COSUser in users:
            return

        hashed_pwd, pwd = generate_hashed_password()
        roles = [COSRole]
        self.user_manager.create_user(COSUser, roles, hashed_pwd)
        self.user_manager.patch_user(
            COSUser,
            [{"op": "replace", "path": "/opendistro_security_roles", "value": roles}],
        )
        self.secrets.put(Scope.APP, self.secrets.password_key(COSUser), pwd)

    def _initialize_security_index(self, admin_secrets: Dict[str, any]) -> None:
        """Run the security_admin script, it creates and initializes the opendistro_security index.

        IMPORTANT: must only run once per cluster, otherwise the index gets overrode
        """
        args = [
            f"-cd {self.opensearch.paths.conf}/opensearch-security/",
            f"-cn {self.app.name}-{self.model.name}",
            f"-h {self.unit_ip}",
            f"-cacert {self.opensearch.paths.certs}/root-ca.cert",
            f"-cert {self.opensearch.paths.certs}/{CertType.APP_ADMIN}.cert",
            f"-key {self.opensearch.paths.certs}/{CertType.APP_ADMIN}.key",
        ]

        admin_key_pwd = admin_secrets.get("key-password", None)
        if admin_key_pwd is not None:
            args.append(f"-keypass {admin_key_pwd}")

        self.status.set(MaintenanceStatus(SecurityIndexInitProgress))
        self.opensearch.run_script(
            "plugins/opensearch-security/tools/securityadmin.sh", " ".join(args)
        )
        self.status.clear(SecurityIndexInitProgress)

    def _get_nodes(self, use_localhost: bool) -> List[Node]:
        """Fetch the list of nodes of the cluster, depending on the requester."""
        # This means it's the first unit on the cluster.
        if self.unit.is_leader() and not self.peers_data.get(
            Scope.APP, "security_index_initialised", False
        ):
            return []

        return ClusterTopology.nodes(self.opensearch, use_localhost, self.alt_hosts)

    def _set_node_conf(self, nodes: List[Node]) -> None:
        """Set the configuration of the current node / unit."""
        # retrieve the updated conf if exists
        update_conf = (self.peers_data.get_object(Scope.APP, "nodes_config") or {}).get(
            self.unit_name
        )
        if update_conf:
            update_conf = Node.from_dict(update_conf)

        # set default generated roles, or the ones passed in the updated conf
        if (
            deployment_desc := self.opensearch_peer_cm.deployment_desc()
        ).start == StartMode.WITH_PROVIDED_ROLES:
            computed_roles = deployment_desc.config.roles
        else:
            computed_roles = (
                update_conf.roles
                if update_conf
                else ClusterTopology.suggest_roles(nodes, self.app.planned_units())
            )

        cm_names = ClusterTopology.get_cluster_managers_names(nodes)
        cm_ips = ClusterTopology.get_cluster_managers_ips(nodes)

        contribute_to_bootstrap = False
        if "cluster_manager" in computed_roles:
            cm_names.append(self.unit_name)
            cm_ips.append(self.unit_ip)

            cms_in_bootstrap = self.peers_data.get(Scope.APP, "bootstrap_contributors_count", 0)
            if cms_in_bootstrap < self.app.planned_units():
                contribute_to_bootstrap = True

                if self.unit.is_leader():
                    self.peers_data.put(
                        Scope.APP, "bootstrap_contributors_count", cms_in_bootstrap + 1
                    )

                # indicates that this unit is part of the "initial cm nodes"
                self.peers_data.put(Scope.UNIT, "bootstrap_contributor", True)

        deployment_desc = self.opensearch_peer_cm.deployment_desc()
        self.opensearch_config.set_node(
            cluster_name=deployment_desc.config.cluster_name,
            unit_name=self.unit_name,
            roles=computed_roles,
            cm_names=cm_names,
            cm_ips=cm_ips,
            contribute_to_bootstrap=contribute_to_bootstrap,
            node_temperature=deployment_desc.config.data_temperature,
        )

    def _cleanup_bootstrap_conf_if_applies(self) -> None:
        """Remove some conf props in the CM nodes that contributed to the cluster bootstrapping."""
        self.peers_data.delete(Scope.UNIT, "bootstrap_contributor")
        self.opensearch_config.cleanup_bootstrap_conf()

    def _add_cm_addresses_to_conf(self):
        """Add the new IP addresses of the current CM units."""
        try:
            # fetch nodes
            nodes = ClusterTopology.nodes(
                self.opensearch, use_localhost=self.opensearch.is_node_up(), hosts=self.alt_hosts
            )
            # update (append) CM IPs
            self.opensearch_config.add_seed_hosts(
                [node.ip for node in nodes if node.is_cm_eligible()]
            )
        except OpenSearchHttpError:
            return

    def _reconfigure_and_restart_unit_if_needed(self):
        """Reconfigure the current unit if a new config was computed for it, then restart."""
        nodes_config = self.peers_data.get_object(Scope.APP, "nodes_config")
        if not nodes_config:
            return

        nodes_config = {name: Node.from_dict(node) for name, node in nodes_config.items()}

        # update (append) CM IPs
        self.opensearch_config.add_seed_hosts(
            [node.ip for node in list(nodes_config.values()) if node.is_cm_eligible()]
        )

        new_node_conf = nodes_config.get(self.unit_name)
        if not new_node_conf:
            # the conf could not be computed / broadcast, because this node is
            # "starting" and is not online "yet" - either barely being configured (i.e. TLS)
            # or waiting to start.
            return

        current_conf = self.opensearch_config.load_node()
        if (
            sorted(current_conf["node.roles"]) == sorted(new_node_conf.roles)
            and current_conf.get("node.attr.temp") == new_node_conf.temperature
        ):
            # no conf change (roles for now)
            return

        self.status.set(WaitingStatus(WaitingToStart))
        self.on[self.service_manager.name].acquire_lock.emit(
            callback_override="_restart_opensearch"
        )

    def _recompute_roles_if_needed(self, event: RelationChangedEvent):
        """Recompute node roles:self-healing that didn't trigger leader related event occurred."""
        try:
            nodes = self._get_nodes(self.opensearch.is_node_up())
            if len(nodes) < self.app.planned_units():
                event.defer()
                return

            self._compute_and_broadcast_updated_topology(nodes)
        except OpenSearchHttpError:
            pass

    def _compute_and_broadcast_updated_topology(self, current_nodes: List[Node]) -> None:
        """Compute cluster topology and broadcast node configs (roles for now) to change if any."""
        if not current_nodes:
            return

        current_reported_nodes = {
            name: Node.from_dict(node)
            for name, node in (self.peers_data.get_object(Scope.APP, "nodes_config") or {}).items()
        }

        if (
            deployment_desc := self.opensearch_peer_cm.deployment_desc()
        ).start == StartMode.WITH_GENERATED_ROLES:
            updated_nodes = ClusterTopology.recompute_nodes_conf(
                app_name=self.app.name, nodes=current_nodes
            )
        else:
            updated_nodes = {
                node.name: Node(
                    name=node.name,
                    roles=deployment_desc.config.roles,
                    ip=node.ip,
                    app_name=self.app.name,
                    temperature=deployment_desc.config.data_temperature,
                )
                for node in current_nodes
            }
            try:
                self.opensearch_peer_cm.validate_roles(current_nodes, on_new_unit=False)
            except OpenSearchProvidedRolesException as e:
                logger.exception(e)
                self.app.status = BlockedStatus(str(e))

        if current_reported_nodes == updated_nodes:
            return

        self.peers_data.put_object(Scope.APP, "nodes_config", updated_nodes)

        # all units will get a peer_rel_changed event, for leader we do as follows
        self._reconfigure_and_restart_unit_if_needed()

    def _check_certs_expiration(self, event: UpdateStatusEvent) -> None:
        """Checks the certificates' expiration."""
        date_format = "%Y-%m-%d %H:%M:%S"
        last_cert_check = datetime.strptime(
            self.peers_data.get(Scope.UNIT, "certs_exp_checked_at", "1970-01-01 00:00:00"),
            date_format,
        )

        # See if the last check was made less than 6h ago, if yes - leave
        if (datetime.now() - last_cert_check).seconds < 6 * 3600:
            return

        certs = self.tls.get_unit_certificates()

        # keep certificates that are expiring in less than 24h
        for cert_type in list(certs.keys()):
            hours = cert_expiration_remaining_hours(certs[cert_type])
            if hours > 24 * 7:
                del certs[cert_type]

        if certs:
            missing = [cert.val for cert in certs.keys()]
            self.status.set(BlockedStatus(CertsExpirationError.format(", ".join(missing))))

            # stop opensearch in case the Node-transport certificate expires.
            if certs.get(CertType.UNIT_TRANSPORT) is not None:
                try:
                    self._stop_opensearch()
                except OpenSearchStopError:
                    event.defer()
                    return

        self.peers_data.put(
            Scope.UNIT, "certs_exp_checked_at", datetime.now().strftime(date_format)
        )

    def _scrape_config(self) -> List[Dict]:
        """Generates the scrape config as needed."""
        app_secrets = self.secrets.get_object(Scope.APP, CertType.APP_ADMIN.val)
        ca = app_secrets.get("ca-cert")
        pwd = self.secrets.get(Scope.APP, self.secrets.password_key(COSUser))
        return [
            {
                "metrics_path": "/_prometheus/metrics",
                "static_configs": [{"targets": [f"{self.unit_ip}:{COSPort}"]}],
                "tls_config": {"ca": ca},
                "scheme": "https" if self._is_tls_fully_configured() else "http",
                "basic_auth": {"username": f"{COSUser}", "password": f"{pwd}"},
            }
        ]

    @abstractmethod
    def store_tls_resources(
        self, cert_type: CertType, secrets: Dict[str, any], override_admin: bool = True
    ):
        """Write certificates and keys on disk."""
        pass

    @abstractmethod
    def _are_all_tls_resources_stored(self):
        """Check if all TLS resources are stored on disk."""
        pass

    @abstractmethod
    def _delete_stored_tls_resources(self):
        """Delete the TLS resources of the unit that are stored on disk."""
        pass

    @property
    def unit_ip(self) -> str:
        """IP address of the current unit."""
        return get_host_ip(self, PeerRelationName)

    @property
    def unit_name(self) -> str:
        """Name of the current unit."""
        return self.unit.name.replace("/", "-")

    @property
    def unit_id(self) -> int:
        """ID of the current unit."""
        return int(self.unit.name.split("/")[1])

    @property
    def alt_hosts(self) -> Optional[List[str]]:
        """Return an alternative host (of another node) in case the current is offline."""
        all_units_ips = units_ips(self, PeerRelationName)
        all_hosts = list(all_units_ips.values())
        random.shuffle(all_hosts)

        if not all_hosts:
            return None

        return reachable_hosts([host for host in all_hosts if host != self.unit_ip])
