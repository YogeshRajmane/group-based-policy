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

import hashlib

from aim.api import resource as aim_resource
from aim import context as aim_context
from aim import utils as aim_utils
from neutron._i18n import _LE
from neutron._i18n import _LI
from neutron.agent.linux import dhcp
from neutron.api.v2 import attributes
from neutron.common import constants as n_constants
from neutron.common import exceptions as n_exc
from neutron import context as n_context
from neutron import manager
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import helpers as log
from oslo_log import log as logging
from oslo_utils import excutils

from gbpservice.neutron.db.grouppolicy import group_policy_mapping_db as gpmdb
from gbpservice.neutron.extensions import cisco_apic
from gbpservice.neutron.extensions import cisco_apic_gbp as aim_ext
from gbpservice.neutron.extensions import cisco_apic_l3
from gbpservice.neutron.extensions import group_policy as gpolicy
from gbpservice.neutron.services.grouppolicy.common import (
    constants as gp_const)
from gbpservice.neutron.services.grouppolicy.common import constants as g_const
from gbpservice.neutron.services.grouppolicy.common import exceptions as exc
from gbpservice.neutron.services.grouppolicy.drivers import (
    neutron_resources as nrd)
from gbpservice.neutron.services.grouppolicy.drivers.cisco.apic import (
    aim_mapping_rpc as aim_rpc)
from gbpservice.neutron.services.grouppolicy.drivers.cisco.apic import (
    apic_mapping as amap)
from gbpservice.neutron.services.grouppolicy.drivers.cisco.apic import (
    apic_mapping_lib as alib)
from gbpservice.neutron.services.grouppolicy import plugin as gbp_plugin


LOG = logging.getLogger(__name__)
FORWARD = 'Forward'
REVERSE = 'Reverse'
FILTER_DIRECTIONS = {FORWARD: False, REVERSE: True}
FORWARD_FILTER_ENTRIES = 'Forward-FilterEntries'
REVERSE_FILTER_ENTRIES = 'Reverse-FilterEntries'
ADDR_SCOPE_KEYS = ['address_scope_v4_id', 'address_scope_v6_id']
AUTO_PTG_NAME_PREFIX = 'auto-ptg-%s'
# Note that this prefix should not exceede 4 characters
AUTO_PTG_PREFIX = 'auto'
AUTO_PTG_ID_PREFIX = AUTO_PTG_PREFIX + '%s'

# Definitions duplicated from apicapi lib
APIC_OWNED = 'apic_owned_'
PROMISCUOUS_TYPES = [n_constants.DEVICE_OWNER_DHCP,
                     n_constants.DEVICE_OWNER_LOADBALANCER]
# TODO(ivar): define a proper promiscuous API
PROMISCUOUS_SUFFIX = 'promiscuous'

CONTRACTS = 'contracts'
CONTRACT_SUBJECTS = 'contract_subjects'
FILTERS = 'filters'
FILTER_ENTRIES = 'filter_entries'

# REVISIT: Auto-PTG is currently config driven to align with the
# config driven behavior of the older driver but is slated for
# removal.
opts = [
    cfg.BoolOpt('create_auto_ptg',
                default=True,
                help=_("Automatically create a PTG when a L2 Policy "
                       "gets created. This is currently an aim_mapping "
                       "policy driver specific feature.")),
]

cfg.CONF.register_opts(opts, "aim_mapping")


class SimultaneousV4V6AddressScopesNotSupportedOnAimDriver(
    exc.GroupPolicyBadRequest):
    message = _("Both v4 and v6 address_scopes cannot be set "
                "simultaneously for a l3_policy.")


class SimultaneousV4V6SubnetpoolsNotSupportedOnAimDriver(
    exc.GroupPolicyBadRequest):
    message = _("Both v4 and v6 subnetpools cannot be set "
                "simultaneously for a l3_policy.")


class InconsistentAddressScopeSubnetpool(exc.GroupPolicyBadRequest):
    message = _("Subnetpool is not associated with the address "
                "scope for a l3_policy.")


class NoAddressScopeForSubnetpool(exc.GroupPolicyBadRequest):
    message = _("Subnetpool does not have an associated address scope.")


class AutoPTGDeleteNotSupported(exc.GroupPolicyBadRequest):
    message = _("Auto PTG %(id)s cannot be deleted.")


class SharedAttributeUpdateNotSupported(exc.GroupPolicyBadRequest):
    message = _("Resource shared attribute update not supported with AIM "
                "GBP driver for resource of type %(type)s")


class AIMMappingDriver(nrd.CommonNeutronBase, aim_rpc.AIMMappingRPCMixin):
    """AIM Mapping Orchestration driver.

    This driver maps GBP resources to the ACI-Integration-Module (AIM).
    """

    @log.log_method_call
    def initialize(self):
        LOG.info(_LI("APIC AIM Policy Driver initializing"))
        super(AIMMappingDriver, self).initialize()
        self._apic_aim_mech_driver = None
        self._apic_segmentation_label_driver = None
        self.create_auto_ptg = cfg.CONF.aim_mapping.create_auto_ptg
        if self.create_auto_ptg:
            LOG.info(_LI('Auto PTG creation configuration set, '
                         'this will result in automatic creation of a PTG '
                         'per L2 Policy'))
        self.setup_opflex_rpc_listeners()
        self._ensure_apic_infra()

    def _ensure_apic_infra(self):
        # TODO(ivar): remove this code from here
        # with the old architecture, this is how we used to create APIC
        # infra model. This is now undesirable for a plethora of reasons,
        # some of which include the fact that we are adding a dependency
        # to apic_ml2, apicapi, and we are also using the old configuration
        # model to make this work. We need to decide how we actually want to
        # infra configuration.
        LOG.debug('Pushing APIC infra configuration')
        amap.ApicMappingDriver.get_apic_manager()

    @property
    def aim_mech_driver(self):
        if not self._apic_aim_mech_driver:
            ml2plus_plugin = manager.NeutronManager.get_plugin()
            self._apic_aim_mech_driver = (
                ml2plus_plugin.mechanism_manager.mech_drivers['apic_aim'].obj)
        return self._apic_aim_mech_driver

    @property
    def aim(self):
        return self.aim_mech_driver.aim

    @property
    def name_mapper(self):
        return self.aim_mech_driver.name_mapper

    @property
    def apic_segmentation_label_driver(self):
        if not self._apic_segmentation_label_driver:
            ext_drivers = self.gbp_plugin.extension_manager.ordered_ext_drivers
            for driver in ext_drivers:
                if 'apic_segmentation_label' == driver.name:
                    self._apic_segmentation_label_driver = (
                        driver.obj)
                    break
        return self._apic_segmentation_label_driver

    @log.log_method_call
    def ensure_tenant(self, plugin_context, tenant_id):
        self.aim_mech_driver.ensure_tenant(plugin_context, tenant_id)

    def aim_display_name(self, name):
        return aim_utils.sanitize_display_name(name)

    @log.log_method_call
    def create_l3_policy_precommit(self, context):
        l3p = context.current
        self._check_l3policy_ext_segment(context, l3p)

        l3p_db = context._plugin._get_l3_policy(
            context._plugin_context, l3p['id'])
        if l3p['address_scope_v4_id'] and l3p['address_scope_v6_id']:
            raise SimultaneousV4V6AddressScopesNotSupportedOnAimDriver()
        if l3p['subnetpools_v4'] and l3p['subnetpools_v6']:
            raise SimultaneousV4V6SubnetpoolsNotSupportedOnAimDriver()
        mix1 = l3p['address_scope_v4_id'] is not None and l3p['subnetpools_v6']
        mix2 = l3p['address_scope_v6_id'] is not None and l3p['subnetpools_v4']
        if mix1 or mix2:
            raise InconsistentAddressScopeSubnetpool()
        ascp = None
        if l3p['address_scope_v6_id'] or l3p['subnetpools_v6']:
            l3p_db['ip_version'] = 6
            context.current['ip_version'] = 6
            ascp = 'address_scope_v6_id'
        elif l3p['address_scope_v4_id'] or l3p['subnetpools_v4']:
            # Since we are not supporting dual stack yet, if both v4 and
            # v6 address_scopes are set, the v4 address_scope will be used
            # to set the l3p ip_version
            l3p_db['ip_version'] = 4
            ascp = 'address_scope_v4_id'
        if not ascp:
            # Explicit address_scope has not been set
            ascp = 'address_scope_v4_id' if l3p_db['ip_version'] == 4 else (
                'address_scope_v6_id')
            if not l3p[ascp]:
                # REVISIT: For dual stack.
                # This logic assumes either 4 or 6 but not both
                self._use_implicit_address_scope(context, clean_session=False)
                l3p_db[ascp] = l3p[ascp]
        else:
            # TODO(Sumit): check that l3p['ip_pool'] does not overlap with an
            # existing subnetpool associated with the explicit address_scope
            pass
        subpool = 'subnetpools_v4' if l3p_db['ip_version'] == 4 else (
            'subnetpools_v6')
        if not l3p[subpool]:
            # REVISIT: For dual stack.
            # This logic assumes either 4 or 6 but not both
            self._use_implicit_subnetpool(
                context, address_scope_id=l3p_db[ascp],
                ip_version=l3p_db['ip_version'], clean_session=False)
        else:
            if len(l3p[subpool]) == 1:
                sp = self._get_subnetpool(
                    context._plugin_context, l3p[subpool][0],
                    clean_session=False)
                if not sp['address_scope_id']:
                    raise NoAddressScopeForSubnetpool()
                if len(sp['prefixes']) == 1:
                    l3p_db['ip_pool'] = sp['prefixes'][0]
                l3p_db[ascp] = sp['address_scope_id']
                l3p_db['subnet_prefix_length'] = int(sp['default_prefixlen'])
            else:
                # TODO(Sumit): There is more than one subnetpool explicitly
                # associated. Unset the ip_pool and subnet_prefix_length. This
                # required changing the DB schema.
                sp_ascp = None
                for sp_id in l3p[subpool]:
                    # REVISIT: For dual stack.
                    # This logic assumes either 4 or 6 but not both
                    sp = self._get_subnetpool(
                        context._plugin_context, sp_id, clean_session=False)
                    if not sp['address_scope_id']:
                        raise NoAddressScopeForSubnetpool()
                    if not sp_ascp:
                        if l3p_db[ascp]:
                            # This is the case where the address_scope
                            # was explicitly set for the l3p  and we need to
                            # check if it conflicts with the address_scope of
                            # the first subnetpool
                            if sp['address_scope_id'] != l3p_db[ascp]:
                                raise InconsistentAddressScopeSubnetpool()
                        else:
                            # No address_scope was explicitly set for the l3p,
                            # so set it to that of the first subnetpool
                            l3p_db[ascp] = sp['address_scope_id']
                        sp_ascp = sp['address_scope_id']
                    elif sp_ascp != sp['address_scope_id']:
                        # all subnetpools do not have the same address_scope
                        raise InconsistentAddressScopeSubnetpool()
                LOG.info(_LI("Since multiple subnetpools are configured for "
                             "this l3_policy, it's ip_pool and "
                             "subnet_prefix_length attributes will be unset."))
                l3p_db['ip_pool'] = None
                l3p_db['subnet_prefix_length'] = None

        # REVISIT: Check if the following constraint still holds
        if len(l3p['routers']) > 1:
            raise exc.L3PolicyMultipleRoutersNotSupported()
        # REVISIT: Validate non overlapping IPs in the same tenant.
        #          Currently this validation is not required for the
        #          AIM driver, and since the AIM driver is the only
        #          driver inheriting from this driver, we are okay
        #          without the check.
        self._reject_invalid_router_access(context, clean_session=False)
        if not l3p['routers']:
            self._use_implicit_router(context, clean_session=False)
        external_segments = context.current['external_segments']
        if external_segments:
            self._plug_l3p_routers_to_ext_segment(context, l3p,
                                                  external_segments)

    @log.log_method_call
    def update_l3_policy_precommit(self, context):
        if (context.current['subnetpools_v4'] or
            context.original['subnetpools_v4']) and (
                context.current['subnetpools_v6'] or
                context.original['subnetpools_v6']):
            raise SimultaneousV4V6SubnetpoolsNotSupportedOnAimDriver()
        if context.current['routers'] != context.original['routers']:
            raise exc.L3PolicyRoutersUpdateNotSupported()
        # Currently there is no support for router update in l3p update.
        # Added this check just in case it is supported in future.
        self._reject_invalid_router_access(context, clean_session=False)
        # TODO(Sumit): For extra safety add validation for address_scope change
        self._check_l3policy_ext_segment(context, context.current)
        old_segment_dict = context.original['external_segments']
        new_segment_dict = context.current['external_segments']
        if (context.current['external_segments'] !=
                context.original['external_segments']):
            new_segments = set(new_segment_dict.keys())
            old_segments = set(old_segment_dict.keys())
            removed = old_segments - new_segments
            self._unplug_l3p_routers_from_ext_segment(context,
                                                      context.current,
                                                      removed)
            added_dict = {s: new_segment_dict[s]
                          for s in (new_segments - old_segments)}
            if added_dict:
                self._plug_l3p_routers_to_ext_segment(context,
                                                      context.current,
                                                      added_dict)

    @log.log_method_call
    def delete_l3_policy_precommit(self, context):
        external_segments = context.current['external_segments']
        if external_segments:
            self._unplug_l3p_routers_from_ext_segment(context,
                context.current, external_segments.keys())
        l3p_db = context._plugin._get_l3_policy(
            context._plugin_context, context.current['id'])
        v4v6subpools = {4: l3p_db.subnetpools_v4, 6: l3p_db.subnetpools_v6}
        for k, v in v4v6subpools.iteritems():
            subpools = [sp.subnetpool_id for sp in v]
            for sp_id in subpools:
                self._db_plugin(
                    context._plugin)._remove_subnetpool_from_l3_policy(
                        context._plugin_context, l3p_db['id'], sp_id,
                        ip_version=k)
                self._cleanup_subnetpool(context._plugin_context, sp_id,
                                         clean_session=False)
        for ascp in ADDR_SCOPE_KEYS:
            if l3p_db[ascp]:
                ascp_id = l3p_db[ascp]
                l3p_db.update({ascp: None})
                self._cleanup_address_scope(context._plugin_context, ascp_id,
                                            clean_session=False)
        for router_id in context.current['routers']:
            self._db_plugin(context._plugin)._remove_router_from_l3_policy(
                context._plugin_context, l3p_db['id'], router_id)
            self._cleanup_router(context._plugin_context, router_id,
                                 clean_session=False)

    @log.log_method_call
    def get_l3_policy_status(self, context):
        # Not all of the neutron resources that l3_policy maps to
        # has a status attribute, hence we derive the status
        # from the AIM resources that the neutron resources map to
        session = context._plugin_context.session
        l3p_db = context._plugin._get_l3_policy(
            context._plugin_context, context.current['id'])
        mapped_aim_resources = []
        # Note: Subnetpool is not mapped to any AIM resource, hence it is not
        # considered for deriving the status
        mapped_status = []

        for ascp in ADDR_SCOPE_KEYS:
            if l3p_db[ascp]:
                ascp_id = l3p_db[ascp]
                ascope = self._get_address_scope(
                    context._plugin_context, ascp_id, clean_session=False)
                vrf_dn = ascope['apic:distinguished_names']['VRF']
                aim_vrf = self._get_vrf_by_dn(context, vrf_dn)
                mapped_aim_resources.append(aim_vrf)

        routers = [router.router_id for router in l3p_db.routers]
        for router_id in routers:
            router = self._get_router(
                context._plugin_context, router_id, clean_session=False)
            mapped_status.append(
                {'status': self._map_ml2plus_status(router)})

        mapped_status.append({'status': self._merge_aim_status(
            session, mapped_aim_resources)})
        context.current['status'] = self._merge_gbp_status(mapped_status)

    @log.log_method_call
    def create_l2_policy_precommit(self, context):
        super(AIMMappingDriver, self).create_l2_policy_precommit(context)
        l2p = context.current
        net = self._get_network(context._plugin_context,
                                l2p['network_id'],
                                clean_session=False)
        default_epg_dn = net['apic:distinguished_names']['EndpointGroup']
        l2p_count = self._db_plugin(context._plugin).get_l2_policies_count(
            context._plugin_context)
        if (l2p_count == 1):
            # This is the first l2p for this tenant hence create the Infra
            # Services and Implicit Contracts and setup the default EPG
            self._create_implicit_contracts_and_configure_default_epg(
                context, l2p, default_epg_dn)
        else:
            # Services and Implicit Contracts already exist for this tenant,
            # only setup the default EPG
            self._configure_contracts_for_default_epg(
                context, l2p, default_epg_dn)
        if self.create_auto_ptg:
            desc = "System created auto PTG for L2P: %s" % l2p['id']
            data = {
                "id": self._get_auto_ptg_id(l2p['id']),
                "name": self._get_auto_ptg_name(l2p),
                "description": desc,
                "l2_policy_id": l2p['id'],
                "proxied_group_id": None,
                "proxy_type": None,
                "proxy_group_id": attributes.ATTR_NOT_SPECIFIED,
                "network_service_policy_id": None,
                "service_management": False,
                "shared": l2p['shared'],
            }
            self._create_policy_target_group(
                context._plugin_context, data, clean_session=False)

    @log.log_method_call
    def delete_l2_policy_precommit(self, context):
        l2p_id = context.current['id']
        l2p_db = context._plugin._get_l2_policy(
            context._plugin_context, l2p_id)
        net = self._get_network(context._plugin_context,
                                l2p_db['network_id'],
                                clean_session=False)
        default_epg_dn = net['apic:distinguished_names']['EndpointGroup']
        auto_ptg_id = self._get_auto_ptg_id(l2p_id)
        try:
            auto_ptg = context._plugin._get_policy_target_group(
                context._plugin_context, auto_ptg_id)
            self._process_subnets_for_ptg_delete(
                context, auto_ptg, l2p_id)
            if auto_ptg['l2_policy_id']:
                auto_ptg.update({'l2_policy_id': None})
            # REVISIT: Consider calling the actual GBP plugin
            # instead of it's base DB mixin class.
            self._db_plugin(
                context._plugin).delete_policy_target_group(
                    context._plugin_context, auto_ptg['id'])
        except gpolicy.PolicyTargetGroupNotFound:
            LOG.info(_LI("Auto PTG with ID %(id)s for "
                         "for L2P %(l2p)s not found. If create_auto_ptg "
                         "configuration was not set at the time of the L2P "
                         "creation, you can safely ignore this, else this "
                         "could potentially be indication of an error."),
                     {'id': auto_ptg_id, 'l2p': l2p_id})
        l2p_count = self._db_plugin(context._plugin).get_l2_policies_count(
            context._plugin_context)
        if (l2p_count == 1):
            self._delete_implicit_contracts_and_unconfigure_default_epg(
                context, context.current, default_epg_dn)
        super(AIMMappingDriver, self).delete_l2_policy_precommit(context)

    @log.log_method_call
    def get_l2_policy_status(self, context):
        l2p_db = context._plugin._get_l2_policy(
            context._plugin_context, context.current['id'])
        net = self._get_network(context._plugin_context,
                                l2p_db['network_id'],
                                clean_session=False)

        if net:
            context.current['status'] = net['status']
            default_epg_dn = net['apic:distinguished_names']['EndpointGroup']
            aim_resources = self._get_implicit_contracts_for_default_epg(
                context, l2p_db, default_epg_dn)
            aim_resources_list = []
            for k in aim_resources.keys():
                if not aim_resources[k] or not all(
                    x for x in aim_resources[k]):
                    # We expected a AIM mapped resource but did not find
                    # it, so something seems to be wrong
                    context.current['status'] = gp_const.STATUS_ERROR
                    return
                aim_resources_list.extend(aim_resources[k])
            merged_aim_status = self._merge_aim_status(
                context._plugin_context.session, aim_resources_list)
            context.current['status'] = self._merge_gbp_status(
                [context.current, {'status': merged_aim_status}])
        else:
            context.current['status'] = gp_const.STATUS_ERROR

    @log.log_method_call
    def create_policy_target_group_precommit(self, context):
        session = context._plugin_context.session

        if self._is_auto_ptg(context.current):
            self._use_implicit_subnet(context)
            return

        if context.current['subnets']:
            raise alib.ExplicitSubnetAssociationNotSupported()

        if not context.current['l2_policy_id']:
            self._create_implicit_l2_policy(context, clean_session=False)
            ptg_db = context._plugin._get_policy_target_group(
                context._plugin_context, context.current['id'])
            ptg_db['l2_policy_id'] = l2p_id = context.current['l2_policy_id']
        else:
            l2p_id = context.current['l2_policy_id']

        l2p_db = context._plugin._get_l2_policy(
            context._plugin_context, l2p_id)

        net = self._get_network(
            context._plugin_context, l2p_db['network_id'],
            clean_session=False)

        self._use_implicit_subnet(context)

        bd_name = str(self.name_mapper.network(
            session, net['id'], net['name']))
        bd_tenant_name = str(self._aim_tenant_name(
            session, context.current['tenant_id']))

        provided_contracts = self._get_aim_contract_names(
            session, context.current['provided_policy_rule_sets'])
        consumed_contracts = self._get_aim_contract_names(
            session, context.current['consumed_policy_rule_sets'])
        aim_epg = self._aim_endpoint_group(
            session, context.current, bd_name, bd_tenant_name,
            provided_contracts=provided_contracts,
            consumed_contracts=consumed_contracts)
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        vmms, phys = self.aim_mech_driver.get_aim_domains(aim_ctx)
        aim_epg.openstack_vmm_domain_names = vmms
        aim_epg.physical_domain_names = phys
        # AIM EPG will be persisted in the following call
        self._add_implicit_svc_contracts_to_epg(context, l2p_db, aim_epg)

    @log.log_method_call
    def update_policy_target_group_precommit(self, context):
        self._reject_shared_update(context, 'policy_target_group')
        session = context._plugin_context.session
        old_provided_contracts = self._get_aim_contract_names(
            session, context.original['provided_policy_rule_sets'])
        old_consumed_contracts = self._get_aim_contract_names(
            session, context.original['consumed_policy_rule_sets'])
        new_provided_contracts = self._get_aim_contract_names(
            session, context.current['provided_policy_rule_sets'])
        new_consumed_contracts = self._get_aim_contract_names(
            session, context.current['consumed_policy_rule_sets'])

        aim_epg = self._get_aim_endpoint_group(session, context.current)
        if aim_epg:
            if not self._is_auto_ptg(context.current):
                aim_epg.display_name = (
                    self.aim_display_name(context.current['name']))
            aim_epg.provided_contract_names = (
                list((set(aim_epg.provided_contract_names) -
                      set(old_provided_contracts)) |
                     set(new_provided_contracts)))
            aim_epg.consumed_contract_names = (
                list((set(aim_epg.consumed_contract_names) -
                      set(old_consumed_contracts)) |
                     set(new_consumed_contracts)))

            self._add_contracts_for_epg(
                aim_context.AimContext(session), aim_epg)

    @log.log_method_call
    def delete_policy_target_group_precommit(self, context):
        plugin_context = context._plugin_context
        auto_ptg_id = self._get_auto_ptg_id(context.current['l2_policy_id'])
        if context.current['id'] == auto_ptg_id:
            raise AutoPTGDeleteNotSupported(id=context.current['id'])
        ptg_db = context._plugin._get_policy_target_group(
            plugin_context, context.current['id'])
        session = context._plugin_context.session

        aim_ctx = self._get_aim_context(context)
        epg = self._aim_endpoint_group(session, context.current)
        self.aim.delete(aim_ctx, epg)
        self._process_subnets_for_ptg_delete(
            context, ptg_db, context.current['l2_policy_id'])

        if ptg_db['l2_policy_id']:
            l2p_id = ptg_db['l2_policy_id']
            ptg_db.update({'l2_policy_id': None})
            l2p_db = context._plugin._get_l2_policy(
                plugin_context, l2p_id)
            if not l2p_db['policy_target_groups'] or (
                (len(l2p_db['policy_target_groups']) == 1) and (
                    self._is_auto_ptg(l2p_db['policy_target_groups'][0]))):
                self._cleanup_l2_policy(context, l2p_id, clean_session=False)

    @log.log_method_call
    def extend_policy_target_group_dict(self, session, result):
        epg = self._aim_endpoint_group(session, result)
        if epg:
            result[cisco_apic.DIST_NAMES] = {cisco_apic.EPG: epg.dn}

    @log.log_method_call
    def get_policy_target_group_status(self, context):
        session = context._plugin_context.session
        epg = self._aim_endpoint_group(session, context.current)
        context.current['status'] = self._map_aim_status(session, epg)

    @log.log_method_call
    def create_policy_target_precommit(self, context):
        if not context.current['port_id']:
            ptg = self._db_plugin(
                context._plugin).get_policy_target_group(
                    context._plugin_context,
                    context.current['policy_target_group_id'])
            subnets = self._get_subnets(
                context._plugin_context, {'id': ptg['subnets']},
                clean_session=False)

            self._use_implicit_port(context, subnets=subnets,
                                    clean_session=False)

    @log.log_method_call
    def update_policy_target_precommit(self, context):
        # TODO(Sumit): Implement
        pass

    @log.log_method_call
    def delete_policy_target_precommit(self, context):
        pt_db = context._plugin._get_policy_target(
            context._plugin_context, context.current['id'])
        if pt_db['port_id']:
            self._cleanup_port(context._plugin_context, pt_db['port_id'])

    @log.log_method_call
    def update_policy_classifier_precommit(self, context):
        o_dir = context.original['direction']
        c_dir = context.current['direction']
        o_prot = context.original['protocol']
        c_prot = context.current['protocol']
        # Process classifier update for direction or protocol change
        if ((o_dir != c_dir) or (
            (o_prot in alib.REVERSIBLE_PROTOCOLS) != (
                c_prot in alib.REVERSIBLE_PROTOCOLS))):
            # TODO(Sumit): Update corresponding AIM FilterEntries
            # and ContractSubjects
            raise Exception

    @log.log_method_call
    def create_policy_rule_precommit(self, context):
        entries = alib.get_filter_entries_for_policy_rule(context)
        if entries['forward_rules']:
            session = context._plugin_context.session
            aim_ctx = self._get_aim_context(context)
            aim_filter = self._aim_filter(session, context.current)
            self.aim.create(aim_ctx, aim_filter)
            self._create_aim_filter_entries(session, aim_ctx, aim_filter,
                                            entries['forward_rules'])
            if entries['reverse_rules']:
                # Also create reverse rule
                aim_filter = self._aim_filter(session, context.current,
                                              reverse_prefix=True)
                self.aim.create(aim_ctx, aim_filter)
                self._create_aim_filter_entries(session, aim_ctx, aim_filter,
                                                entries['reverse_rules'])

    @log.log_method_call
    def update_policy_rule_precommit(self, context):
        self.delete_policy_rule_precommit(context)
        self.create_policy_rule_precommit(context)

    @log.log_method_call
    def delete_policy_rule_precommit(self, context):
        session = context._plugin_context.session
        aim_ctx = self._get_aim_context(context)
        aim_filter = self._aim_filter(session, context.current)
        aim_reverse_filter = self._aim_filter(
            session, context.current, reverse_prefix=True)
        for afilter in [aim_filter, aim_reverse_filter]:
            self._delete_aim_filter_entries(aim_ctx, afilter)
            self.aim.delete(aim_ctx, afilter)
        self.name_mapper.delete_apic_name(session, context.current['id'])

    @log.log_method_call
    def extend_policy_rule_dict(self, session, result):
        result[cisco_apic.DIST_NAMES] = {}
        aim_filter_entries = self._get_aim_filter_entries(session, result)
        for k, v in aim_filter_entries.iteritems():
            dn_list = []
            for entry in v:
                dn_list.append(entry.dn)
            if k == FORWARD:
                result[cisco_apic.DIST_NAMES].update(
                    {aim_ext.FORWARD_FILTER_ENTRIES: dn_list})
            else:
                result[cisco_apic.DIST_NAMES].update(
                    {aim_ext.REVERSE_FILTER_ENTRIES: dn_list})

    @log.log_method_call
    def get_policy_rule_status(self, context):
        session = context._plugin_context.session
        aim_filters = self._get_aim_filters(session, context.current)
        aim_filter_entries = self._get_aim_filter_entries(
            session, context.current)
        context.current['status'] = self._merge_aim_status(
            session, aim_filters.values() + aim_filter_entries.values())

    @log.log_method_call
    def create_policy_rule_set_precommit(self, context):
        if context.current['child_policy_rule_sets']:
            raise alib.HierarchicalContractsNotSupported()
        aim_ctx = self._get_aim_context(context)
        session = context._plugin_context.session
        aim_contract = self._aim_contract(session, context.current)
        self.aim.create(aim_ctx, aim_contract)
        rules = self._db_plugin(context._plugin).get_policy_rules(
            context._plugin_context,
            filters={'id': context.current['policy_rules']})
        self._populate_aim_contract_subject(context, aim_contract, rules)

    @log.log_method_call
    def update_policy_rule_set_precommit(self, context):
        if context.current['child_policy_rule_sets']:
            raise alib.HierarchicalContractsNotSupported()
        session = context._plugin_context.session
        aim_contract = self._aim_contract(session, context.current)
        rules = self._db_plugin(context._plugin).get_policy_rules(
            context._plugin_context,
            filters={'id': context.current['policy_rules']})
        self._populate_aim_contract_subject(
            context, aim_contract, rules)

    @log.log_method_call
    def delete_policy_rule_set_precommit(self, context):
        aim_ctx = self._get_aim_context(context)
        session = context._plugin_context.session
        aim_contract = self._aim_contract(session, context.current)
        self._delete_aim_contract_subject(aim_ctx, aim_contract)
        self.aim.delete(aim_ctx, aim_contract)
        self.name_mapper.delete_apic_name(session, context.current['id'])

    @log.log_method_call
    def extend_policy_rule_set_dict(self, session, result):
        result[cisco_apic.DIST_NAMES] = {}
        aim_contract = self._aim_contract(session, result)
        aim_contract_subject = self._aim_contract_subject(aim_contract)
        result[cisco_apic.DIST_NAMES].update(
            {aim_ext.CONTRACT: aim_contract.dn,
             aim_ext.CONTRACT_SUBJECT: aim_contract_subject.dn})

    @log.log_method_call
    def get_policy_rule_set_status(self, context):
        session = context._plugin_context.session
        aim_contract = self._aim_contract(session, context.current)
        aim_contract_subject = self._aim_contract_subject(aim_contract)
        context.current['status'] = self._merge_aim_status(
            session, [aim_contract, aim_contract_subject])

    @log.log_method_call
    def create_external_segment_precommit(self, context):
        if not context.current['subnet_id']:
            raise exc.ImplicitSubnetNotSupported()
        subnet = self._get_subnet(context._plugin_context,
                                  context.current['subnet_id'])
        network = self._get_network(context._plugin_context,
                                    subnet['network_id'])
        if not network['router:external']:
            raise exc.InvalidSubnetForES(sub_id=subnet['id'],
                                         net_id=network['id'])
        db_es = context._plugin._get_external_segment(
                context._plugin_context, context.current['id'])
        db_es.cidr = subnet['cidr']
        db_es.ip_version = subnet['ip_version']
        context.current['cidr'] = db_es.cidr
        context.current['ip_version'] = db_es.ip_version

        cidrs = sorted([x['destination']
                        for x in context.current['external_routes']])
        self._update_network(context._plugin_context,
                             subnet['network_id'],
                             {cisco_apic.EXTERNAL_CIDRS: cidrs},
                             clean_session=False)

    @log.log_method_call
    def update_external_segment_precommit(self, context):
        # REVISIT: what other attributes should we prevent an update on?
        invalid = ['port_address_translation']
        for attr in invalid:
            if context.current[attr] != context.original[attr]:
                raise exc.InvalidAttributeUpdateForES(attribute=attr)

        old_cidrs = sorted([x['destination']
                            for x in context.original['external_routes']])
        new_cidrs = sorted([x['destination']
                            for x in context.current['external_routes']])
        if old_cidrs != new_cidrs:
            subnet = self._get_subnet(context._plugin_context,
                                      context.current['subnet_id'])
            self._update_network(context._plugin_context,
                                 subnet['network_id'],
                                 {cisco_apic.EXTERNAL_CIDRS: new_cidrs},
                                 clean_session=False)

    @log.log_method_call
    def delete_external_segment_precommit(self, context):
        subnet = self._get_subnet(context._plugin_context,
                                  context.current['subnet_id'])
        self._update_network(context._plugin_context,
                             subnet['network_id'],
                             {cisco_apic.EXTERNAL_CIDRS: ['0.0.0.0/0']},
                             clean_session=False)

    @log.log_method_call
    def create_external_policy_precommit(self, context):
        self._check_external_policy(context, context.current)

        routers = self._get_ext_policy_routers(context,
            context.current, context.current['external_segments'])
        for r in routers:
            self._set_router_ext_contracts(context, r, context.current)

    @log.log_method_call
    def update_external_policy_precommit(self, context):
        ep = context.current
        old_ep = context.original
        self._check_external_policy(context, ep)
        removed_segments = (set(old_ep['external_segments']) -
                            set(ep['external_segments']))
        added_segment = (set(ep['external_segments']) -
                         set(old_ep['external_segments']))
        if removed_segments:
            routers = self._get_ext_policy_routers(context, ep,
                                                   removed_segments)
            for r in routers:
                self._set_router_ext_contracts(context, r, None)
        if (added_segment or
            sorted(old_ep['provided_policy_rule_sets']) !=
                sorted(ep['provided_policy_rule_sets']) or
            sorted(old_ep['consumed_policy_rule_sets']) !=
                sorted(ep['consumed_policy_rule_sets'])):
            routers = self._get_ext_policy_routers(context, ep,
                                                   ep['external_segments'])
            for r in routers:
                self._set_router_ext_contracts(context, r, ep)

    @log.log_method_call
    def delete_external_policy_precommit(self, context):
        routers = self._get_ext_policy_routers(context,
            context.current, context.current['external_segments'])
        for r in routers:
            self._set_router_ext_contracts(context, r, None)

    def _reject_shared_update(self, context, type):
        if context.original.get('shared') != context.current.get('shared'):
            raise SharedAttributeUpdateNotSupported(type=type)

    def _aim_tenant_name(self, session, tenant_id):
        # TODO(ivar): manage shared objects
        tenant_name = self.name_mapper.tenant(session, tenant_id)
        LOG.debug("Mapped tenant_id %(id)s to %(apic_name)s",
                  {'id': tenant_id, 'apic_name': tenant_name})
        return tenant_name

    def _aim_endpoint_group(self, session, ptg, bd_name=None,
                            bd_tenant_name=None,
                            provided_contracts=None,
                            consumed_contracts=None):
        # This returns a new AIM EPG resource
        # TODO(Sumit): Use _aim_resource_by_name
        tenant_id = ptg['tenant_id']
        tenant_name = self._aim_tenant_name(session, tenant_id)
        id = ptg['id']
        name = ptg['name']
        epg_name = self.apic_epg_name_for_policy_target_group(
            session, id, name)
        display_name = self.aim_display_name(ptg['name'])
        LOG.debug("Mapped ptg_id %(id)s with name %(name)s to %(apic_name)s",
                  {'id': id, 'name': name, 'apic_name': epg_name})
        kwargs = {'tenant_name': str(tenant_name),
                  'name': str(epg_name),
                  'display_name': display_name,
                  'app_profile_name': self.aim_mech_driver.ap_name}
        if bd_name:
            kwargs['bd_name'] = bd_name
        if bd_tenant_name:
            kwargs['bd_tenant_name'] = bd_tenant_name

        if provided_contracts:
            kwargs['provided_contract_names'] = provided_contracts

        if consumed_contracts:
            kwargs['consumed_contract_names'] = consumed_contracts

        epg = aim_resource.EndpointGroup(**kwargs)
        return epg

    def _get_aim_endpoint_group(self, session, ptg):
        # This gets an EPG from the AIM DB
        epg = self._aim_endpoint_group(session, ptg)
        aim_ctx = aim_context.AimContext(session)
        epg_fetched = self.aim.get(aim_ctx, epg)
        if not epg_fetched:
            LOG.debug("No EPG found in AIM DB")
        else:
            LOG.debug("Got epg: %s", epg_fetched.__dict__)
        return epg_fetched

    def _aim_filter(self, session, pr, reverse_prefix=False):
        # This returns a new AIM Filter resource
        # TODO(Sumit): Use _aim_resource_by_name
        tenant_id = pr['tenant_id']
        tenant_name = self._aim_tenant_name(session, tenant_id)
        id = pr['id']
        name = pr['name']
        display_name = self.aim_display_name(pr['name'])
        if reverse_prefix:
            filter_name = self.name_mapper.policy_rule(
                session, id, resource_name=name, prefix=alib.REVERSE_PREFIX)
        else:
            filter_name = self.name_mapper.policy_rule(session, id,
                                                       resource_name=name)
        LOG.debug("Mapped policy_rule_id %(id)s with name %(name)s to",
                  "%(apic_name)s",
                  {'id': id, 'name': name, 'apic_name': filter_name})
        kwargs = {'tenant_name': str(tenant_name),
                  'name': str(filter_name),
                  'display_name': display_name}

        aim_filter = aim_resource.Filter(**kwargs)
        return aim_filter

    def _aim_filter_entry(self, session, aim_filter, filter_entry_name,
                          filter_entry_attrs):
        # This returns a new AIM FilterEntry resource
        # TODO(Sumit): Use _aim_resource_by_name
        tenant_name = aim_filter.tenant_name
        filter_name = aim_filter.name
        display_name = self.aim_display_name(filter_name)
        kwargs = {'tenant_name': tenant_name,
                  'filter_name': filter_name,
                  'name': filter_entry_name,
                  'display_name': display_name}
        kwargs.update(filter_entry_attrs)

        aim_filter_entry = aim_resource.FilterEntry(**kwargs)
        return aim_filter_entry

    def _delete_aim_filter_entries(self, aim_context, aim_filter):
        aim_filter_entries = self.aim.find(
            aim_context, aim_resource.FilterEntry,
            tenant_name=aim_filter.tenant_name,
            filter_name=aim_filter.name)
        for entry in aim_filter_entries:
            self.aim.delete(aim_context, entry)

    def _create_aim_filter_entries(self, session, aim_ctx, aim_filter,
                                   filter_entries):
        for k, v in filter_entries.iteritems():
            self._create_aim_filter_entry(
                session, aim_ctx, aim_filter, k, v)

    def _create_aim_filter_entry(self, session, aim_ctx, aim_filter,
                                 filter_entry_name, filter_entry_attrs,
                                 overwrite=False):
        aim_filter_entry = self._aim_filter_entry(
            session, aim_filter, filter_entry_name,
            alib.map_to_aim_filter_entry(filter_entry_attrs))
        self.aim.create(aim_ctx, aim_filter_entry, overwrite)

    def _get_aim_filters(self, session, policy_rule):
        # This gets the Forward and Reverse Filters from the AIM DB
        aim_ctx = aim_context.AimContext(session)
        filters = {}
        for k, v in FILTER_DIRECTIONS.iteritems():
            aim_filter = self._aim_filter(session, policy_rule, v)
            aim_filter_fetched = self.aim.get(aim_ctx, aim_filter)
            if not aim_filter_fetched:
                LOG.debug("No %s Filter found in AIM DB", k)
            else:
                LOG.debug("Got %s Filter: %s",
                          (aim_filter_fetched.__dict__, k))
            filters[k] = aim_filter_fetched
        return filters

    def _get_aim_filter_names(self, session, policy_rule):
        # Forward and Reverse AIM Filter names for a Policy Rule
        aim_filters = self._get_aim_filters(session, policy_rule)
        aim_filter_names = [f.name for f in aim_filters.values()]
        return aim_filter_names

    def _get_aim_filter_entries(self, session, policy_rule):
        # This gets the Forward and Reverse FilterEntries from the AIM DB
        aim_ctx = aim_context.AimContext(session)
        filters = self._get_aim_filters(session, policy_rule)
        filters_entries = {}
        for k, v in filters.iteritems():
            aim_filter_entries = self.aim.find(
                aim_ctx, aim_resource.FilterEntry,
                tenant_name=v.tenant_name, filter_name=v.name)
            if not aim_filter_entries:
                LOG.debug("No %s FilterEntry found in AIM DB", k)
            else:
                LOG.debug("Got %s FilterEntry: %s",
                          (aim_filter_entries, k))
            filters_entries[k] = aim_filter_entries
        return filters_entries

    def _aim_contract(self, session, policy_rule_set):
        # This returns a new AIM Contract resource
        tenant_id = policy_rule_set['tenant_id']
        id = policy_rule_set['id']
        name = policy_rule_set['name']
        return self._aim_resource_by_name(
            session, gpolicy.POLICY_RULE_SETS, aim_resource.Contract,
            tenant_id, gbp_resource_id=id, gbp_resource_name=name)

    def _aim_contract_subject(self, aim_contract, in_filters=None,
                              out_filters=None, bi_filters=None):
        # This returns a new AIM ContractSubject resource
        # TODO(Sumit): Use _aim_resource_by_name
        if not in_filters:
            in_filters = []
        if not out_filters:
            out_filters = []
        if not bi_filters:
            bi_filters = []
        display_name = self.aim_display_name(aim_contract.name)
        # Since we create one ContractSubject per Contract,
        # ContractSubject is given the Contract name
        kwargs = {'tenant_name': aim_contract.tenant_name,
                  'contract_name': aim_contract.name,
                  'name': aim_contract.name,
                  'display_name': display_name,
                  'in_filters': in_filters,
                  'out_filters': out_filters,
                  'bi_filters': bi_filters}

        aim_contract_subject = aim_resource.ContractSubject(**kwargs)
        return aim_contract_subject

    def _populate_aim_contract_subject(self, context, aim_contract,
                                       policy_rules):
        in_filters, out_filters, bi_filters = [], [], []
        session = context._plugin_context.session
        for rule in policy_rules:
            aim_filters = self._get_aim_filter_names(session, rule)
            classifier = context._plugin.get_policy_classifier(
                context._plugin_context, rule['policy_classifier_id'])
            if classifier['direction'] == g_const.GP_DIRECTION_IN:
                in_filters += aim_filters
            elif classifier['direction'] == g_const.GP_DIRECTION_OUT:
                out_filters += aim_filters
            else:
                bi_filters += aim_filters
        self._populate_aim_contract_subject_by_filters(
            context, aim_contract, in_filters, out_filters, bi_filters)

    def _populate_aim_contract_subject_by_filters(
        self, context, aim_contract, in_filters=None, out_filters=None,
        bi_filters=None):
        if not in_filters:
            in_filters = []
        if not out_filters:
            out_filters = []
        if not bi_filters:
            bi_filters = []
        aim_ctx = self._get_aim_context(context)
        aim_contract_subject = self._aim_contract_subject(
            aim_contract, in_filters, out_filters, bi_filters)
        self.aim.create(aim_ctx, aim_contract_subject, overwrite=True)

    def _get_aim_contract(self, session, policy_rule_set):
        # This gets a Contract from the AIM DB
        aim_ctx = aim_context.AimContext(session)
        contract = self._aim_contract(session, policy_rule_set)
        contract_fetched = self.aim.get(aim_ctx, contract)
        if not contract_fetched:
            LOG.debug("No Contract found in AIM DB")
        else:
            LOG.debug("Got Contract: %s", contract_fetched.__dict__)
        return contract_fetched

    def _get_aim_contract_names(self, session, prs_id_list):
        contract_list = []
        for prs_id in prs_id_list:
            contract_name = self.name_mapper.policy_rule_set(session, prs_id)
            contract_list.append(contract_name)
        return contract_list

    def _get_aim_contract_subject(self, session, policy_rule_set):
        # This gets a ContractSubject from the AIM DB
        aim_ctx = aim_context.AimContext(session)
        contract = self._aim_contract(session, policy_rule_set)
        contract_subject = self._aim_contract_subject(contract)
        contract_subject_fetched = self.aim.get(aim_ctx, contract_subject)
        if not contract_subject_fetched:
            LOG.debug("No Contract found in AIM DB")
        else:
            LOG.debug("Got ContractSubject: %s",
                      contract_subject_fetched.__dict__)
        return contract_subject_fetched

    def _delete_aim_contract_subject(self, aim_context, aim_contract):
        aim_contract_subject = self._aim_contract_subject(aim_contract)
        self.aim.delete(aim_context, aim_contract_subject)

    def _get_aim_default_endpoint_group(self, session, network):
        epg_name = self.name_mapper.network(session, network['id'],
                                            network['name'])
        tenant_name = self.name_mapper.tenant(session, network['tenant_id'])
        aim_ctx = aim_context.AimContext(session)
        epg = aim_resource.EndpointGroup(
            tenant_name=tenant_name,
            app_profile_name=self.aim_mech_driver.ap_name, name=epg_name)
        return self.aim.get(aim_ctx, epg)

    def _aim_bridge_domain(self, session, tenant_id, network_id, network_name):
        # This returns a new AIM BD resource
        # TODO(Sumit): Use _aim_resource_by_name
        tenant_name = self._aim_tenant_name(session, tenant_id)
        bd_name = self.name_mapper.network(session, network_id, network_name)
        display_name = self.aim_display_name(network_name)
        LOG.info(_LI("Mapped network_id %(id)s with name %(name)s to "
                     "%(apic_name)s"),
                 {'id': network_id, 'name': network_name,
                  'apic_name': bd_name})

        bd = aim_resource.BridgeDomain(tenant_name=str(tenant_name),
                                       name=str(bd_name),
                                       display_name=display_name)
        return bd

    def _get_l2p_subnets(self, context, l2p_id, clean_session=False):
        plugin_context = context._plugin_context
        l2p = context._plugin.get_l2_policy(plugin_context, l2p_id)
        # REVISIT: The following should be a get_subnets call via local API
        return self._core_plugin.get_subnets_by_network(
            plugin_context, l2p['network_id'])

    def _sync_ptg_subnets(self, context, l2p):
        l2p_subnets = [x['id'] for x in
                       self._get_l2p_subnets(context, l2p['id'])]
        ptgs = context._plugin._get_policy_target_groups(
            context._plugin_context.elevated(), {'l2_policy_id': [l2p['id']]})
        for sub in l2p_subnets:
            # Add to PTG
            for ptg in ptgs:
                if sub not in ptg['subnets']:
                    try:
                        (context._plugin.
                         _add_subnet_to_policy_target_group(
                             context._plugin_context.elevated(),
                             ptg['id'], sub))
                    except gpolicy.PolicyTargetGroupNotFound as e:
                        LOG.warning(e)

    def _use_implicit_subnet(self, context, force_add=False,
                             clean_session=False):
        """Implicit subnet for AIM.

        The first PTG in a L2P will allocate a new subnet from the L3P.
        Any subsequent PTG in the same L2P will use the same subnet.
        Additional subnets will be allocated as and when the currently used
        subnet runs out of IP addresses.
        """
        l2p_id = context.current['l2_policy_id']
        with lockutils.lock(l2p_id, external=True):
            subs = self._get_l2p_subnets(context, l2p_id)
            subs = set([x['id'] for x in subs])
            added = []
            if not subs or force_add:
                l2p = context._plugin.get_l2_policy(
                    context._plugin_context, l2p_id)
                name = APIC_OWNED + l2p['name']
                added = super(
                    AIMMappingDriver,
                    self)._use_implicit_subnet_from_subnetpool(
                        context, subnet_specifics={'name': name},
                        clean_session=clean_session)
            context.add_subnets(subs - set(context.current['subnets']))
            if added:
                self._sync_ptg_subnets(context, l2p)
                l3p = self._get_l3p_for_l2policy(context, l2p_id)
                for r in l3p['routers']:
                    self._attach_router_to_subnets(context._plugin_context,
                                                   r, added)

    def _create_implicit_contracts_and_configure_default_epg(
        self, context, l2p, epg_dn):
        self._process_contracts_for_default_epg(context, l2p, epg_dn)

    def _configure_contracts_for_default_epg(self, context, l2p, epg_dn):
        self._process_contracts_for_default_epg(
            context, l2p, epg_dn, create=False, delete=False)

    def _delete_implicit_contracts_and_unconfigure_default_epg(
        self, context, l2p, epg_dn):
        self._process_contracts_for_default_epg(
            context, l2p, epg_dn, create=False, delete=True)

    def _get_implicit_contracts_for_default_epg(
        self, context, l2p, epg_dn):
        return self._process_contracts_for_default_epg(
            context, l2p, epg_dn, get=True)

    def _process_contracts_for_default_epg(
        self, context, l2p, epg_dn, create=True, delete=False, get=False):
        # get=True overrides the create and delete cases, and returns a dict
        # with the Contracts, ContractSubjects, Filters, and FilterEntries
        # for the default EPG
        # create=True, delete=False means create everything and add Contracts
        # to the default EPG
        # create=False, delete=False means only add Contracts to the default
        # EPG
        # create=False, delete=True means only remove Contracts from the
        # default EPG and delete them
        # create=True, delete=True is not a valid combination
        if create and delete:
            LOG.error(_LE("Incorrect use of internal method "
                          "_process_contracts_for_default_epg(), create and "
                          "delete cannot be True at the same time"))
            raise
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        aim_epg = self.aim.get(aim_ctx,
                               aim_resource.EndpointGroup.from_dn(epg_dn))

        # Infra Services' FilterEntries and attributes
        infra_entries = alib.get_service_contract_filter_entries()
        # ARP FilterEntry and attributes
        arp_entries = alib.get_arp_filter_entry()
        contracts = {alib.SERVICE_PREFIX: infra_entries,
                     alib.IMPLICIT_PREFIX: arp_entries}

        for contract_name_prefix, entries in contracts.iteritems():
            contract_name = str(self.name_mapper.policy_rule_set(
                session, l2p['tenant_id'], l2p['tenant_id'],
                prefix=contract_name_prefix))
            # Create Contract (one per tenant)
            # REVIST(Sumit): Naming convention used for this Filter
            aim_contract = self._aim_resource_by_name(
                session, 'tenant', aim_resource.Contract,
                l2p['tenant_id'], gbp_resource_id=l2p['tenant_id'],
                gbp_resource_name=alib.PER_PROJECT,
                prefix=contract_name_prefix)

            if get:
                aim_resources = {}
                aim_resources[FILTERS] = []
                aim_resources[FILTER_ENTRIES] = []
                aim_resources[CONTRACT_SUBJECTS] = []
                contract_fetched = self.aim.get(aim_ctx, aim_contract)
                aim_resources[CONTRACTS] = [contract_fetched]
            else:
                if create:
                    self.aim.create(aim_ctx, aim_contract, overwrite=True)

                if not delete:
                    # Add Contracts to the default EPG
                    if contract_name_prefix == alib.IMPLICIT_PREFIX:
                        # Default EPG provides and consumes ARP Contract
                        self._add_contracts_for_epg(
                            aim_ctx, aim_epg,
                            provided_contracts=[contract_name],
                            consumed_contracts=[contract_name])
                    else:
                        # Default EPG provides Infra Services' Contract
                        self._add_contracts_for_epg(
                            aim_ctx, aim_epg,
                            provided_contracts=[contract_name])

            filter_names = []
            for k, v in entries.iteritems():
                # Create Filter (one per tenant)
                # REVIST(Sumit): Naming convention used for this Filter
                aim_filter = self._aim_resource_by_name(
                    session, 'tenant', aim_resource.Filter,
                    l2p['tenant_id'], gbp_resource_id=l2p['tenant_id'],
                    gbp_resource_name=alib.PER_PROJECT,
                    prefix=''.join([contract_name_prefix, k, '-']))
                if get:
                    filter_fetched = self.aim.get(aim_ctx, aim_filter)
                    aim_resources[FILTERS].append(filter_fetched)
                    aim_filter_entry = self._aim_filter_entry(
                        session, aim_filter, k,
                        alib.map_to_aim_filter_entry(v))
                    entry_fetched = self.aim.get(aim_ctx, aim_filter_entry)
                    aim_resources[FILTER_ENTRIES].append(entry_fetched)
                else:
                    if create:
                        self.aim.create(aim_ctx, aim_filter, overwrite=True)
                        # Create FilterEntries (one per tenant) and associate
                        #  with Filter
                        self._create_aim_filter_entry(
                            session, aim_ctx, aim_filter, k, v, overwrite=True)
                        filter_names.append(aim_filter.name)
                    if delete:
                        self._delete_aim_filter_entries(aim_ctx, aim_filter)
                        self.aim.delete(aim_ctx, aim_filter)
            if get:
                aim_contract_subject = self._aim_contract_subject(aim_contract)
                subject_fetched = self.aim.get(aim_ctx, aim_contract_subject)
                aim_resources[CONTRACT_SUBJECTS].append(subject_fetched)
                return aim_resources
            else:
                if create:
                    # Create ContractSubject (one per tenant) with relevant
                    # Filters, and associate with Contract
                    self._populate_aim_contract_subject_by_filters(
                        context, aim_contract, bi_filters=filter_names)
                if delete:
                    self._delete_aim_contract_subject(aim_ctx, aim_contract)
                    self.aim.delete(aim_ctx, aim_contract)

    def _add_implicit_svc_contracts_to_epg(self, context, l2p, aim_epg):
        session = context._plugin_context.session
        aim_ctx = aim_context.AimContext(session)
        implicit_contract_name = str(self.name_mapper.policy_rule_set(
            session, l2p['tenant_id'], l2p['tenant_id'],
            prefix=alib.IMPLICIT_PREFIX))
        service_contract_name = str(self.name_mapper.policy_rule_set(
            session, l2p['tenant_id'], l2p['tenant_id'],
            prefix=alib.SERVICE_PREFIX))
        self._add_contracts_for_epg(aim_ctx, aim_epg, consumed_contracts=[
            implicit_contract_name, service_contract_name])

    def _add_contracts_for_epg(self, aim_ctx, aim_epg, provided_contracts=None,
                               consumed_contracts=None):
        if provided_contracts:
            aim_epg.provided_contract_names += provided_contracts

        if consumed_contracts:
            aim_epg.consumed_contract_names += consumed_contracts
        self.aim.create(aim_ctx, aim_epg, overwrite=True)

    def _aim_resource_by_name(self, session, gbp_resource, aim_resource_class,
                              tenant_id, gbp_resource_id=None,
                              gbp_resource_name=None, prefix=None):
        kwargs = {'session': session}
        if gbp_resource_id:
            kwargs['resource_id'] = gbp_resource_id
        if gbp_resource_name:
            kwargs['resource_name'] = gbp_resource_name
        if prefix:
            kwargs['prefix'] = prefix
        # TODO(Sumit): Current only PRS is mapped via this method. Once
        # name_mapper is resource independent, change the following call
        # and use for other aim resource object creation.
        aim_name = self.name_mapper.policy_rule_set(**kwargs)
        tenant_name = self._aim_tenant_name(session, tenant_id)
        LOG.debug("Mapped %(gbp_resource)s with id: %(id)s, name: %(name)s ",
                  "prefix: %(prefix)s tenant_name: %(tenant_name)s to "
                  "aim_name: %(aim_name)s",
                  {'gbp_resource': gbp_resource, 'id': gbp_resource_id,
                   'name': gbp_resource_name, 'prefix': prefix,
                   'aim_name': aim_name})
        display_name = self.aim_display_name(gbp_resource_name)
        kwargs = {'tenant_name': str(tenant_name),
                  'name': str(aim_name),
                  'display_name': display_name}

        aim_resource = aim_resource_class(**kwargs)
        return aim_resource

    def _merge_gbp_status(self, gbp_resource_list):
        merged_status = gp_const.STATUS_ACTIVE
        for gbp_resource in gbp_resource_list:
            if gbp_resource['status'] == gp_const.STATUS_BUILD:
                merged_status = gp_const.STATUS_BUILD
            elif gbp_resource['status'] == gp_const.STATUS_ERROR:
                merged_status = gp_const.STATUS_ERROR
                break
        return merged_status

    def _map_ml2plus_status(self, sync_status):
        if not sync_status:
            # REVIST(Sumit)
            return gp_const.STATUS_BUILD
        if sync_status == cisco_apic.SYNC_ERROR:
            return gp_const.STATUS_ERROR
        elif sync_status == cisco_apic.SYNC_BUILD:
            return gp_const.STATUS_BUILD
        else:
            return gp_const.STATUS_ACTIVE

    def _process_subnets_for_ptg_delete(self, context, ptg, l2p_id):
        session = context._plugin_context.session
        plugin_context = context._plugin_context
        subnet_ids = [assoc['subnet_id'] for assoc in ptg['subnets']]

        context._plugin._remove_subnets_from_policy_target_group(
            plugin_context, ptg['id'])
        if subnet_ids:
            for subnet_id in subnet_ids:
                # clean-up subnet if this is the last PTG using the L2P
                if not context._plugin._get_ptgs_for_subnet(
                    plugin_context, subnet_id):
                    if l2p_id:
                        l3p = self._get_l3p_for_l2policy(context, l2p_id)
                        for router_id in l3p['routers']:
                            # If the subnet interface for this router has
                            # already been removed (say manually), the
                            # call to Neutron's remove_router_interface
                            # will cause the transaction to exit immediately.
                            # To avoid this, we first check if this subnet
                            # still has an interface on this router.
                            if self._get_router_interface_port_by_subnet(
                                plugin_context, router_id, subnet_id):
                                with session.begin(nested=True):
                                    self._detach_router_from_subnets(
                                        plugin_context, router_id, [subnet_id])
                    self._cleanup_subnet(plugin_context, subnet_id,
                                         clean_session=False)

    def _map_aim_status(self, session, aim_resource_obj):
        # Note that this implementation assumes that this driver
        # is the only policy driver configured, and no merging
        # with any previous status is required.
        aim_ctx = aim_context.AimContext(session)
        aim_status = self.aim.get_status(aim_ctx, aim_resource_obj)
        if not aim_status:
            # REVIST(Sumit)
            return gp_const.STATUS_BUILD
        if aim_status.is_error():
            return gp_const.STATUS_ERROR
        elif aim_status.is_build():
            return gp_const.STATUS_BUILD
        else:
            return gp_const.STATUS_ACTIVE

    def _merge_aim_status(self, session, aim_resource_obj_list):
        # Note that this implementation assumes that this driver
        # is the only policy driver configured, and no merging
        # with any previous status is required.
        # When merging states of multiple AIM objects, the status
        # priority is ERROR > BUILD > ACTIVE.
        merged_status = gp_const.STATUS_ACTIVE
        for aim_obj in aim_resource_obj_list:
            status = self._map_aim_status(session, aim_obj)
            if status != gp_const.STATUS_ACTIVE:
                merged_status = status
            if merged_status == gp_const.STATUS_ERROR:
                break
        return merged_status

    def _db_plugin(self, plugin_obj):
            return super(gbp_plugin.GroupPolicyPlugin, plugin_obj)

    def _get_aim_context(self, context):
        if hasattr(context, 'session'):
            session = context.session
        else:
            session = context._plugin_context.session
        return aim_context.AimContext(session)

    def _is_port_promiscuous(self, plugin_context, port):
        pt = self._port_id_to_pt(plugin_context, port['id'])
        if (pt and pt.get('cluster_id') and
                pt.get('cluster_id') != pt['id']):
            master = self._get_policy_target(plugin_context, pt['cluster_id'])
            if master.get('group_default_gateway'):
                return True
        return (port['device_owner'] in PROMISCUOUS_TYPES or
                port['name'].endswith(PROMISCUOUS_SUFFIX)) or (
                    pt and pt.get('group_default_gateway'))

    def _is_dhcp_optimized(self, plugin_context, port):
        return self.aim_mech_driver.enable_dhcp_opt

    def _is_metadata_optimized(self, plugin_context, port):
        return self.aim_mech_driver.enable_metadata_opt

    def _get_port_epg(self, plugin_context, port):
        ptg, pt = self._port_id_to_ptg(plugin_context, port['id'])
        if ptg:
            return self._get_aim_endpoint_group(plugin_context.session, ptg)
        else:
            # Return default EPG based on network
            network = self._get_network(plugin_context, port['network_id'])
            epg = self._get_aim_default_endpoint_group(plugin_context.session,
                                                       network)
            if not epg:
                # Something is wrong, default EPG doesn't exist.
                # TODO(ivar): should rise an exception
                LOG.error(_LE("Default EPG doesn't exist for "
                              "port %s"), port['id'])
            return epg

    def _get_subnet_details(self, plugin_context, port, details):
        # L2P might not exist for a pure Neutron port
        l2p = self._network_id_to_l2p(plugin_context, port['network_id'])
        # TODO(ivar): support shadow network
        # if not l2p and self._ptg_needs_shadow_network(context, ptg):
        #    l2p = self._get_l2_policy(context._plugin_context,
        #                              ptg['l2_policy_id'])

        subnets = self._get_subnets(
            plugin_context,
            filters={'id': [ip['subnet_id'] for ip in port['fixed_ips']]})
        for subnet in subnets:
            dhcp_ips = set()
            for port in self._get_ports(
                    plugin_context,
                    filters={
                        'network_id': [subnet['network_id']],
                        'device_owner': [n_constants.DEVICE_OWNER_DHCP]}):
                dhcp_ips |= set([x['ip_address'] for x in port['fixed_ips']
                                 if x['subnet_id'] == subnet['id']])
            dhcp_ips = list(dhcp_ips)
            if not subnet['dns_nameservers']:
                # Use DHCP namespace port IP
                subnet['dns_nameservers'] = dhcp_ips
            # Set Default & Metadata routes if needed
            default_route = metadata_route = {}
            if subnet['ip_version'] == 4:
                for route in subnet['host_routes']:
                    if route['destination'] == '0.0.0.0/0':
                        default_route = route
                    if route['destination'] == dhcp.METADATA_DEFAULT_CIDR:
                        metadata_route = route
                if l2p and not l2p['inject_default_route']:
                    # In this case we do not want to send the default route
                    # and the metadata route. We also do not want to send
                    # the gateway_ip for the subnet.
                    if default_route:
                        subnet['host_routes'].remove(default_route)
                    if metadata_route:
                        subnet['host_routes'].remove(metadata_route)
                    del subnet['gateway_ip']
                else:
                    # Set missing routes
                    if not default_route:
                        subnet['host_routes'].append(
                            {'destination': '0.0.0.0/0',
                             'nexthop': subnet['gateway_ip']})
                    if not metadata_route and dhcp_ips and (
                        not self._is_metadata_optimized(plugin_context, port)):
                        subnet['host_routes'].append(
                            {'destination': dhcp.METADATA_DEFAULT_CIDR,
                             'nexthop': dhcp_ips[0]})
            subnet['dhcp_server_ips'] = dhcp_ips
        return subnets

    def _get_aap_details(self, plugin_context, port, details):
        pt = self._port_id_to_pt(plugin_context, port['id'])
        aaps = port['allowed_address_pairs']
        if pt:
            # Set the correct address ownership for this port
            owned_addresses = self._get_owned_addresses(
                plugin_context, pt['port_id'])
            for allowed in aaps:
                if allowed['ip_address'] in owned_addresses:
                    # Signal the agent that this particular address is active
                    # on its port
                    allowed['active'] = True
        return aaps

    def _get_port_address_scope(self, plugin_context, port):
        for ip in port['fixed_ips']:
            subnet = self._get_subnet(plugin_context, ip['subnet_id'])
            subnetpool = self._get_subnetpools(
                plugin_context, filters={'id': [subnet['subnetpool_id']]})
            if subnetpool:
                address_scope = self._get_address_scopes(
                    plugin_context,
                    filters={'id': [subnetpool[0]['address_scope_id']]})
                if address_scope:
                    return address_scope[0]

    def _get_port_address_scope_cached(self, plugin_context, port, cache):
        if not cache.get('gbp_map_address_scope'):
            cache['gbp_map_address_scope'] = (
                self._get_port_address_scope(plugin_context, port))
        return cache['gbp_map_address_scope']

    def _get_address_scope_cached(self, plugin_context, vrf_id, cache):
        if not cache.get('gbp_map_address_scope'):
            address_scope = self._get_address_scopes(
                plugin_context, filters={'id': [vrf_id]})
            cache['gbp_map_address_scope'] = (address_scope[0] if
                                              address_scope else None)
        return cache['gbp_map_address_scope']

    def _get_vrf_id(self, plugin_context, port, details):
        # retrieve the Address Scope from the Neutron port
        address_scope = self._get_port_address_scope_cached(
            plugin_context, port, details['_cache'])
        # TODO(ivar): what should we return if Address Scope doesn't exist?
        return address_scope['id'] if address_scope else None

    def _get_port_vrf(self, plugin_context, vrf_id, details):
        address_scope = self._get_address_scope_cached(
            plugin_context, vrf_id, details['_cache'])
        if address_scope:
            vrf_name = self.name_mapper.address_scope(
                plugin_context.session, address_scope['id'],
                address_scope['name'])
            tenant_name = self.name_mapper.tenant(
                plugin_context.session, address_scope['tenant_id'])
            aim_ctx = aim_context.AimContext(plugin_context.session)
            epg = aim_resource.VRF(tenant_name=tenant_name, name=vrf_name)
            return self.aim.get(aim_ctx, epg)

    def _get_vrf_subnets(self, plugin_context, vrf_id, details):
        subnets = []
        address_scope = self._get_address_scope_cached(
            plugin_context, vrf_id, details['_cache'])
        if address_scope:
            # Get all the subnetpools associated with this Address Scope
            subnetpools = self._get_subnetpools(
                plugin_context,
                filters={'address_scope_id': [address_scope['id']]})
            for pool in subnetpools:
                subnets.extend(pool['prefixes'])
        return subnets

    def _get_segmentation_labels(self, plugin_context, port, details):
        pt = self._port_id_to_pt(plugin_context, port['id'])
        if self.apic_segmentation_label_driver and pt and (
            'segmentation_labels' in pt):
            return pt['segmentation_labels']

    def _get_nat_details(self, plugin_context, port, host, details):
        """ Add information about IP mapping for DNAT/SNAT """

        fips = []
        ipms = []
        host_snat_ips = []

        # Find all external networks connected to the port.
        # Handle them depending on whether there is a FIP on that
        # network.
        ext_nets = []

        port_sn = set([x['subnet_id'] for x in port['fixed_ips']])
        router_intf_ports = self._get_ports(
            plugin_context,
            filters={'device_owner': [n_constants.DEVICE_OWNER_ROUTER_INTF],
                     'fixed_ips': {'subnet_id': port_sn}})
        if router_intf_ports:
            routers = self._get_routers(
                plugin_context,
                filters={'device_id': [x['device_id']
                                       for x in router_intf_ports]})
            ext_nets = self._get_networks(
                plugin_context,
                filters={'id': [r['external_gateway_info']['network_id']
                                for r in routers
                                if r.get('external_gateway_info')]})
        if not ext_nets:
            return fips, ipms, host_snat_ips

        # Handle FIPs of owned addresses - find other ports in the
        # network whose address is owned by this port.
        # If those ports have FIPs, then steal them.
        fips_filter = [port['id']]
        active_addrs = [a['ip_address']
                        for a in details['allowed_address_pairs']
                        if a['active']]
        if active_addrs:
            others = self._get_ports(
                plugin_context,
                filters={'network_id': [port['network_id']],
                         'fixed_ips': {'ip_address': active_addrs}})
            fips_filter.extend([p['id'] for p in others])
        fips = self._get_fips(plugin_context,
                              filters={'port_id': fips_filter})

        for ext_net in ext_nets:
            dn = ext_net.get(cisco_apic.DIST_NAMES, {}).get(
                cisco_apic.EXTERNAL_NETWORK)
            ext_net_epg_dn = ext_net.get(cisco_apic.DIST_NAMES, {}).get(
                cisco_apic.EPG)
            if not dn or not ext_net_epg_dn:
                continue
            if 'distributed' != ext_net.get(cisco_apic.NAT_TYPE):
                continue

            # TODO(amitbose) Handle per-tenant NAT EPG
            ext_net_epg = aim_resource.EndpointGroup.from_dn(ext_net_epg_dn)

            fips_in_ext_net = filter(
                lambda x: x['floating_network_id'] == ext_net['id'], fips)
            if not fips_in_ext_net:
                ext_segment_name = dn.replace('/', ':')
                ipms.append({'external_segment_name': ext_segment_name,
                             'nat_epg_name': ext_net_epg.name,
                             'nat_epg_tenant': ext_net_epg.tenant_name})
                # TODO(amitbose) Set next_hop_ep_tenant for per-tenant NAT EPG
                if host:
                    snat_ip = self.aim_mech_driver.get_or_allocate_snat_ip(
                        plugin_context, host, ext_net)
                    if snat_ip:
                        snat_ip['external_segment_name'] = ext_segment_name
                        host_snat_ips.append(snat_ip)
            else:
                for f in fips_in_ext_net:
                    f['nat_epg_name'] = ext_net_epg.name
                    f['nat_epg_tenant'] = ext_net_epg.tenant_name
        return fips, ipms, host_snat_ips

    def _get_vrf_by_dn(self, context, vrf_dn):
        aim_context = self._get_aim_context(context)
        vrf = self.aim.get(
            aim_context, aim_resource.VRF.from_dn(vrf_dn))
        return vrf

    def _check_l3policy_ext_segment(self, context, l3policy):
        if l3policy['external_segments']:
            for allocations in l3policy['external_segments'].values():
                if len(allocations) > 1:
                    raise alib.OnlyOneAddressIsAllowedPerExternalSegment()
            # if NAT is disabled, allow only one L3P per ES
            ess = context._plugin.get_external_segments(
                context._plugin_context,
                filters={'id': l3policy['external_segments'].keys()})
            for es in ess:
                ext_net = self._ext_segment_2_ext_network(context, es)
                if (ext_net and
                    ext_net.get(cisco_apic.NAT_TYPE) in
                        ('distributed', 'edge')):
                    continue
                if [x for x in es['l3_policies'] if x != l3policy['id']]:
                    raise alib.OnlyOneL3PolicyIsAllowedPerExternalSegment()

    def _check_external_policy(self, context, ep):
        if ep.get('shared', False):
            # REVISIT(amitbose) This could be relaxed
            raise alib.SharedExternalPolicyUnsupported()
        ess = context._plugin.get_external_segments(
            context._plugin_context,
            filters={'id': ep['external_segments']})
        for es in ess:
            other_eps = context._plugin.get_external_policies(
                context._plugin_context,
                filters={'id': es['external_policies'],
                         'tenant_id': [ep['tenant_id']]})
            if [x for x in other_eps if x['id'] != ep['id']]:
                raise alib.MultipleExternalPoliciesForL3Policy()

    def _get_l3p_subnets(self, context, l3policy):
        l2p_sn = []
        for l2p_id in l3policy['l2_policies']:
            l2p_sn.extend(self._get_l2p_subnets(context, l2p_id))
        return l2p_sn

    def _ext_segment_2_ext_network(self, context, ext_segment):
        subnet = self._get_subnet(context._plugin_context,
                                  ext_segment['subnet_id'])
        if subnet:
            return self._get_network(context._plugin_context,
                                     subnet['network_id'])

    def _map_ext_segment_to_routers(self, context, ext_segments,
                                    routers):
        net_to_router = {r['external_gateway_info']['network_id']: r
                         for r in routers
                         if r.get('external_gateway_info')}
        result = {}
        for es in ext_segments:
            sn = self._get_subnet(context._plugin_context, es['subnet_id'])
            router = net_to_router.get(sn['network_id']) if sn else None
            if router:
                result[es['id']] = router
        return result

    def _plug_l3p_routers_to_ext_segment(self, context, l3policy,
                                         ext_seg_info):
        plugin_context = context._plugin_context
        es_list = self._get_external_segments(plugin_context,
            filters={'id': ext_seg_info.keys()})
        l3p_subs = self._get_l3p_subnets(context, l3policy)

        # REVISIT: We are not re-using the first router created
        # implicitly for the L3Policy (or provided explicitly by the
        # user). Consider using that for the first external segment

        for es in es_list:
            router_id = self._use_implicit_router(context,
                  router_name=l3policy['name'] + '-' + es['name'])
            router = self._create_router_gw_for_external_segment(
                context._plugin_context, es, ext_seg_info, router_id)
            if not ext_seg_info[es['id']] or not ext_seg_info[es['id']][0]:
                # Update L3P assigned address
                efi = router['external_gateway_info']['external_fixed_ips']
                assigned_ips = [x['ip_address'] for x in efi
                                if x['subnet_id'] == es['subnet_id']]
                context.set_external_fixed_ips(es['id'], assigned_ips)
            if es['external_policies']:
                ext_policy = self._get_external_policies(plugin_context,
                   filters={'id': es['external_policies'],
                            'tenant_id': [l3policy['tenant_id']]})
                if ext_policy:
                    self._set_router_ext_contracts(context, router_id,
                                                   ext_policy[0])
            # Use admin context because router and subnet may be in
            # different tenants
            self._attach_router_to_subnets(plugin_context.elevated(),
                                           router_id, l3p_subs)

    def _unplug_l3p_routers_from_ext_segment(self, context, l3policy,
                                             ext_seg_ids):
        plugin_context = context._plugin_context
        es_list = self._get_external_segments(plugin_context,
                                              filters={'id': ext_seg_ids})
        routers = self._get_routers(plugin_context,
                                    filters={'id': l3policy['routers']})
        es_2_router = self._map_ext_segment_to_routers(context, es_list,
                                                       routers)
        for r in es_2_router.values():
            router_subs = self._get_router_interface_subnets(plugin_context,
                                                             r['id'])
            self._detach_router_from_subnets(plugin_context, r['id'],
                                             router_subs)
            context.remove_router(r['id'])
            self._cleanup_router(plugin_context, r['id'],
                                 clean_session=False)

    def _get_router_interface_subnets(self, plugin_context, router_id):
        router_ports = self._get_ports(plugin_context,
            filters={'device_owner': [n_constants.DEVICE_OWNER_ROUTER_INTF],
                     'device_id': [router_id]})
        return set(y['subnet_id']
                   for x in router_ports for y in x['fixed_ips'])

    def _get_router_interface_port_by_subnet(self, plugin_context,
                                             router_id, subnet_id):
        router_ports = self._get_ports(plugin_context,
            filters={'device_owner': [n_constants.DEVICE_OWNER_ROUTER_INTF],
                     'device_id': [router_id],
                     'fixed_ips': {'subnet_id': [subnet_id]}},
                                       clean_session=False)
        return (router_ports or [None])[0]

    def _attach_router_to_subnets(self, plugin_context, router_id, subs):
        rtr_sn = self._get_router_interface_subnets(plugin_context, router_id)
        for subnet in subs:
            if subnet['id'] in rtr_sn:  # already attached
                continue
            gw_port = self._get_ports(plugin_context,
               filters={'fixed_ips': {'ip_address': [subnet['gateway_ip']],
                                      'subnet_id': [subnet['id']]}})
            if gw_port:
                # Gateway port is in use, create new interface port
                attrs = {'tenant_id': subnet['tenant_id'],
                         'network_id': subnet['network_id'],
                         'fixed_ips': [{'subnet_id': subnet['id']}],
                         'device_id': '',
                         'device_owner': '',
                         'mac_address': attributes.ATTR_NOT_SPECIFIED,
                         'name': '%s-%s' % (router_id, subnet['id']),
                         'admin_state_up': True}
                try:
                    intf_port = self._create_port(plugin_context, attrs,
                                                  clean_session=False)
                except n_exc.NeutronException:
                    with excutils.save_and_reraise_exception():
                        LOG.exception(_LE('Failed to create explicit router '
                                          'interface port in subnet '
                                          '%(subnet)s'),
                                      {'subnet': subnet['id']})
                interface_info = {'port_id': intf_port['id']}
                try:
                    self._add_router_interface(plugin_context, router_id,
                                               interface_info)
                except n_exc.BadRequest:
                    self._delete_port(plugin_context, intf_port['id'],
                                      clean_session=False)
                    with excutils.save_and_reraise_exception():
                        LOG.exception(_LE('Attaching router %(router)s to '
                                          '%(subnet)s with explicit port '
                                          '%(port) failed'),
                                      {'subnet': subnet['id'],
                                       'router': router_id,
                                       'port': intf_port['id']})
            else:
                self._plug_router_to_subnet(plugin_context, subnet['id'],
                                            router_id)

    def _detach_router_from_subnets(self, plugin_context, router_id, sn_ids):
        for subnet_id in sn_ids:
            # Use admin context because router and subnet may be in
            # different tenants
            self._remove_router_interface(plugin_context.elevated(),
                                          router_id,
                                          {'subnet_id': subnet_id},
                                          clean_session=False)

    def _set_router_ext_contracts(self, context, router_id, ext_policy):
        session = context._plugin_context.session
        prov = []
        cons = []
        if ext_policy:
            prov = self._get_aim_contract_names(session,
                ext_policy['provided_policy_rule_sets'])
            cons = self._get_aim_contract_names(session,
                ext_policy['consumed_policy_rule_sets'])
        attr = {cisco_apic_l3.EXTERNAL_PROVIDED_CONTRACTS: prov,
                cisco_apic_l3.EXTERNAL_CONSUMED_CONTRACTS: cons}
        self._update_router(context._plugin_context, router_id, attr,
                            clean_session=False)

    def _get_ext_policy_routers(self, context, ext_policy, ext_seg_ids):
        plugin_context = context._plugin_context
        es = self._get_external_segments(plugin_context,
                                         filters={'id': ext_seg_ids})
        subs = self._get_subnets(context._plugin_context,
            filters={'id': [e['subnet_id'] for e in es]})
        ext_net = {s['network_id'] for s in subs}
        l3ps = set([l3p for e in es for l3p in e['l3_policies']])
        l3ps = self._get_l3_policies(plugin_context,
             filters={'id': l3ps,
                      'tenant_id': [ext_policy['tenant_id']]})
        routers = self._get_routers(plugin_context,
            filters={'id': [r for l in l3ps for r in l['routers']]})
        return [r['id'] for r in routers
            if (r['external_gateway_info'] or {}).get('network_id') in ext_net]

    def _get_auto_ptg_name(self, l2p):
        return AUTO_PTG_NAME_PREFIX % l2p['id']

    def _get_auto_ptg_id(self, l2p_id):
        return AUTO_PTG_ID_PREFIX % hashlib.md5(l2p_id).hexdigest()

    def _is_auto_ptg(self, ptg):
        return ptg['id'].startswith(AUTO_PTG_PREFIX)

    def _get_epg_name_from_dn(self, context, epg_dn):
        aim_context = self._get_aim_context(context)
        default_epg_name = self.aim.get(
            aim_context, aim_resource.EndpointGroup.from_dn(epg_dn)).name
        return default_epg_name

    def apic_epg_name_for_policy_target_group(self, session, ptg_id,
                                              name=None):
        ptg_db = session.query(gpmdb.PolicyTargetGroupMapping).filter_by(
            id=ptg_id).first()
        if ptg_db and self._is_auto_ptg(ptg_db):
            l2p_db = session.query(gpmdb.L2PolicyMapping).filter_by(
                id=ptg_db['l2_policy_id']).first()
            network_id = l2p_db['network_id']
            admin_context = n_context.get_admin_context()
            admin_context._session = session
            net = self._get_network(admin_context, network_id,
                                    clean_session=False)
            default_epg_dn = net['apic:distinguished_names']['EndpointGroup']
            default_epg_name = self._get_epg_name_from_dn(
                admin_context, default_epg_dn)
            return default_epg_name
        else:
            return ptg_id
