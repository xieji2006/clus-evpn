""" CLUS-EVPN - NSO EVPN project for Cisco Live US 2017
"""
from __future__ import absolute_import, division, print_function
from builtins import (ascii, bytes, chr, dict, filter, hex, input, int, map, next, oct, open, pow, range, round,
                      super, zip)
from builtins import str as text
import ncs
from ncs.application import Service, PlanComponent
import ncs.template
import functools
from itertools import islice, chain, repeat
from vxlan.utils import apply_template, NcsServiceConfigError, value_or_empty, get_device_asn, init_plan


# --------------------------------------------------
# Service create callback decorator
# --------------------------------------------------
def vxlan_service(cb_create_method):
    """
    Decorator for cb_create callback. Initialize a self plan component.
    :param cb_create_method: cb_create method
    :return: cb_create wrapper
    """
    @functools.wraps(cb_create_method)
    def wrapper(self, tctx, root, service, proplist):
        self.log.info('Service create(service={})'.format(service))
        self_plan = init_plan(PlanComponent(service, 'self', 'ncs:self'))

        try:
            if cb_create_method(self, tctx, root, service, proplist, self_plan):
                return
        except NcsServiceConfigError as e:
            self.log.error(e)
            self_plan.set_failed('ncs:ready')
        else:
            self_plan.set_reached('ncs:ready')

    return wrapper


# ------------------------------------
# L2 VXLAN TOPOLOGY SERVICE CALLBACK
# ------------------------------------
class VxlanL2ServiceCallback(Service):
    @Service.create
    @vxlan_service
    def cb_create(self, tctx, root, service, proplist, self_plan):
        common_vars = {
            'NVE_SOURCE': value_or_empty(root.plant_information.global_config.nve_source_interface),
        }

        for leaf in service.ports.leaf_node:
            self.log.info('Rendering L2 leaf template for {}'.format(leaf.node_name))
            apply_template('l2_leaf_node', leaf, common_vars)

        border_leaf_nodes = root.plant_information.plant[service.dc_name].border_leaf_node

        if not ((len(service.dci.vlan) == 1) or (len(service.dci.vlan) == len(border_leaf_nodes))):
            raise NcsServiceConfigError('Number of L2 DCI VLANs must be 1 or match the number of border-leaf nodes')

        last_dci_vlan = None
        for border_leaf, dci_vlan in zip(border_leaf_nodes,
                                         islice(chain(service.dci.vlan, repeat(None)), len(border_leaf_nodes))):
            self.log.info('Rendering L2 border-leaf template for {}'.format(border_leaf.name))

            if dci_vlan is None:
                dci_vlan = last_dci_vlan
            else:
                last_dci_vlan = dci_vlan

            border_leaf_vars = {
                'DEVICE': border_leaf.name,
                'DCI_VLAN': dci_vlan.id,
                'DCI_VLAN_NAME': dci_vlan.name
            }
            border_leaf_vars.update(common_vars)
            apply_template('l2_border_leaf_node', service, border_leaf_vars)

            self.log.info('Rendering border-leaf vlan template for {}'.format(border_leaf.name))
            dci_ports = border_leaf.dci_layer2.interface.Port_channel or border_leaf.dci_layer2.interface.Ethernet
            if len(dci_ports) != 1:
                raise NcsServiceConfigError('Each border-leaf can only have one L2 DCI port')
            for dci_port in dci_ports:
                border_leaf_vlan_vars = {
                    'DCI_PORT': dci_port.id,
                }
                border_leaf_vlan_vars.update(border_leaf_vars)
                apply_template('border_leaf_node_vlans', dci_port, border_leaf_vlan_vars)


# ------------------------------------
# L3 VXLAN TOPOLOGY SERVICE CALLBACK
# ------------------------------------
class VxlanL3ServiceCallback(Service):
    @Service.create
    @vxlan_service
    def cb_create(self, tctx, root, service, proplist, self_plan):
        common_vars = {
            'NVE_SOURCE': value_or_empty(root.plant_information.global_config.nve_source_interface),
            'PREFIX-TAG': value_or_empty(root.plant_information.global_config.tenant_prefix_tag),
            'REDIST-STATIC': value_or_empty(root.plant_information.global_config.tenant_route_maps.bgp_redistribute_static),
            'REDIST-CONNECTED': value_or_empty(root.plant_information.global_config.tenant_route_maps.bgp_redistribute_connected),
        }

        for leaf in service.ports.leaf_node:
            self.log.info('Rendering L3 leaf template for {}'.format(leaf.node_name))

            leaf_vars = {
                'DEVICE-ASN': get_device_asn(root, leaf.node_name),
            }
            leaf_vars.update(common_vars)
            apply_template('l3_leaf_node', leaf, leaf_vars)

        dci_vlans = [vlan.id for vlan in service.dci.vlan]

        for border_leaf in root.plant_information.plant[service.dc_name].border_leaf_node:
            self.log.info('Rendering L3 border-leaf template for {}'.format(border_leaf.name))
            border_leaf_vars = {
                'DEVICE': border_leaf.name,
                'DEVICE-ASN': get_device_asn(root, border_leaf.name),
            }
            border_leaf_vars.update(common_vars)
            apply_template('l3_border_leaf_node', service, border_leaf_vars)

            self.log.info('Rendering border-leaf vlan template for {}'.format(border_leaf.name))
            dci_ports = border_leaf.dci_layer3.interface.Port_channel or border_leaf.dci_layer3.interface.Ethernet
            if len(dci_ports) != len(dci_vlans):
                raise NcsServiceConfigError('Number of DCI VLANs must match number of L3 DCI ports')
            for dci_port, dci_vlan in zip(dci_ports, dci_vlans):
                border_leaf_vlan_vars = {
                    'DEVICE': border_leaf.name,
                    'DCI_PORT': dci_port.id,
                    'DCI_VLAN': dci_vlan,
                }
                apply_template('border_leaf_node_vlans', dci_port, border_leaf_vlan_vars)


# ---------------------------------------------
# COMPONENT THREAD THAT WILL BE STARTED BY NCS.
# ---------------------------------------------
class Main(ncs.application.Application):
    def setup(self):
        self.log.info('VXLAN Service RUNNING')

        # Registration of service callbacks
        self.register_service('vxlan-l2-servicepoint', VxlanL2ServiceCallback)
        self.register_service('vxlan-l3-servicepoint', VxlanL3ServiceCallback)

    def teardown(self):
        self.log.info('VXLAN Service FINISHED')


