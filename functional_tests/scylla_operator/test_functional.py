#!/usr/bin/env python

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2021 ScyllaDB

import logging
import random
import pytest

from sdcm.mgmt import TaskStatus  # pylint: disable=import-error


log = logging.getLogger()


def test_cassandra_rackdc(cassandra_rackdc_properties):
    """
    Test of applying cassandra-rackdc.properties via configmap
    """

    with cassandra_rackdc_properties() as props:
        original = props['prefer_local']

    log.info("cassandra-rackdc.properties.prefer_local = %s", original)

    if original == 'false':
        changed = 'true'
    elif original == 'true':
        changed = 'false'
    else:
        assert False, f'cassandra-rackdc.properties have unexpected prefer_local value {original}'

    with cassandra_rackdc_properties() as props:
        assert original == props['prefer_local']
        props['prefer_local'] = changed

    with cassandra_rackdc_properties() as props:
        assert changed == props['prefer_local']
        props['prefer_local'] = original

    with cassandra_rackdc_properties() as props:
        assert original == props['prefer_local']


def test_rolling_restart_cluster(db_cluster):
    db_cluster.k8s_cluster.kubectl("rollout restart statefulset", namespace=db_cluster.namespace)
    for statefulset in db_cluster.statefulsets:
        db_cluster.k8s_cluster.kubectl(
            f"rollout status statefulset/{statefulset.metadata.name} --watch=true --timeout={10}m",
            namespace=db_cluster.namespace,
            timeout=10 * 60 + 10)
    db_cluster.wait_for_pods_readiness(pods_to_wait=1, total_pods=len(db_cluster.nodes))


def test_grow_shrink_cluster(db_cluster):
    new_node = db_cluster.add_nodes(count=1, dc_idx=0, enable_auto_bootstrap=True, rack=0)[0]
    db_cluster.decommission(new_node)
    db_cluster.wait_for_pods_readiness(pods_to_wait=1, total_pods=len(db_cluster.nodes))


@pytest.mark.require_node_terminate('drain_k8s_node')
def test_drain_and_replace_node_kubernetes(db_cluster):
    target_node = random.choice(db_cluster.non_seed_nodes)
    old_uid = target_node.k8s_pod_uid
    log.info('TerminateNode %s (uid=%s)', target_node, old_uid)
    target_node.drain_k8s_node()
    target_node.mark_to_be_replaced()
    target_node.wait_till_k8s_pod_get_uid(ignore_uid=old_uid)
    target_node.wait_for_pod_readiness()
    db_cluster.wait_for_pods_readiness(pods_to_wait=1, total_pods=len(db_cluster.nodes))


@pytest.mark.require_node_terminate('drain_k8s_node')
def test_drain_wait_and_replace_node_kubernetes(db_cluster):
    target_node = random.choice(db_cluster.non_seed_nodes)
    old_uid = target_node.k8s_pod_uid
    log.info('TerminateNode %s (uid=%s)', target_node, old_uid)
    target_node.drain_k8s_node()
    target_node.wait_till_k8s_pod_get_uid(ignore_uid=old_uid)
    old_uid = target_node.k8s_pod_uid
    log.info('Mark %s (uid=%s) to be replaced', target_node, old_uid)
    target_node.mark_to_be_replaced()
    target_node.wait_till_k8s_pod_get_uid(ignore_uid=old_uid)
    target_node.wait_for_pod_readiness()
    db_cluster.wait_for_pods_readiness(pods_to_wait=1, total_pods=len(db_cluster.nodes))


@pytest.mark.require_node_terminate('drain_k8s_node')
def test_drain_terminate_decommission_add_node_kubernetes(db_cluster):
    target_node = random.choice(db_cluster.non_seed_nodes)
    target_node.drain_k8s_node()
    db_cluster.decommission(target_node)
    db_cluster.add_nodes(count=1, dc_idx=0, enable_auto_bootstrap=True, rack=0)
    db_cluster.wait_for_pods_readiness(pods_to_wait=1, total_pods=len(db_cluster.nodes))


@pytest.mark.require_mgmt()
def test_mgmt_repair(db_cluster):
    mgr_cluster = db_cluster.get_cluster_manager()
    mgr_task = mgr_cluster.create_repair_task()
    task_final_status = mgr_task.wait_and_get_final_status(timeout=86400)  # timeout is 24 hours
    assert task_final_status == TaskStatus.DONE, 'Task: {} final status is: {}.'.format(
        mgr_task.id, str(mgr_task.status))


@pytest.mark.require_mgmt()
def test_mgmt_backup(db_cluster):
    mgr_cluster = db_cluster.get_cluster_manager()
    backup_bucket_location = db_cluster.params.get('backup_bucket_location')
    bucket_name = f"s3:{backup_bucket_location.split()[0]}"
    mgr_task = mgr_cluster.create_backup_task(location_list=[bucket_name, ])
    status = mgr_task.wait_and_get_final_status(timeout=54000, step=5, only_final=True)
    assert TaskStatus.DONE == status


def test_listen_address(db_cluster):
    """
    Issues: https://github.com/scylladb/scylla-operator/issues/484
            https://github.com/scylladb/scylla/issues/8381
    Fix commit: https://github.com/scylladb/scylla-operator/pull/529
    """
    all_errors = []
    for node in db_cluster.nodes:
        result = node.remoter.run("ps aux | grep docker-entrypoint.py | grep -Po '\\-\\-listen-address=[^ ]+' | "
                                  "sed -r 's/--listen-address[= ]([^ ]+)/\\1/'", ignore_status=True)
        # Check in command line first
        if result.ok:
            if result.stdout.strip() != '0.0.0.0':
                all_errors.append(f"Node {node.name} has wrong listen-address argument {result.stdout}")
            continue
        # If no --listen-address in command line then proceed with scylla.yaml
        with node.actual_scylla_yaml() as scylla_yaml:
            listen_address = scylla_yaml.get('listen_address')
            if not listen_address:
                all_errors.append(f"Not found listen_address flag in the {node.name} scylla.yaml")
            if listen_address != '0.0.0.0':
                all_errors.append(f'Node {node.name} has wrong listen_address "{listen_address}" in scylla.yaml')

    assert not all_errors, "Following errors found:\n{'\n'.join(errors)}"
