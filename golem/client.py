import atexit
import logging
import sys
import time
import uuid
from collections import Iterable
from copy import copy
from os import path, makedirs
from threading import Lock

from pydispatch import dispatcher
from twisted.internet import task
from twisted.internet.defer import inlineCallbacks, returnValue, gatherResults,\
    Deferred

from golem.appconfig import AppConfig, PUBLISH_BALANCE_INTERVAL, \
    PUBLISH_TASKS_INTERVAL
from golem.clientconfigdescriptor import ClientConfigDescriptor, ConfigApprover
from golem.config.presets import HardwarePresetsMixin
from golem.core.common import to_unicode
from golem.core.fileshelper import du
from golem.core.hardware import HardwarePresets
from golem.core.keysauth import EllipticalKeysAuth
from golem.core.simpleenv import get_local_datadir
from golem.core.simpleserializer import DictSerializer
from golem.core.variables import APP_VERSION
from golem.diag.service import DiagnosticsService, DiagnosticsOutputFormat
from golem.diag.vm import VMDiagnosticsProvider
from golem.environments.environmentsmanager import EnvironmentsManager
from golem.manager.nodestatesnapshot import NodeStateSnapshot
from golem.model import Database, Account
from golem.monitor.model.nodemetadatamodel import NodeMetadataModel
from golem.monitor.monitor import SystemMonitor
from golem.monitorconfig import MONITOR_CONFIG
from golem.network.hyperdrive.daemon_manager import HyperdriveDaemonManager
from golem.network.p2p.node import Node
from golem.network.p2p.p2pservice import P2PService
from golem.network.p2p.peersession import PeerSessionInfo
from golem.network.transport.tcpnetwork import SocketAddress
from golem.ranking.helper.trust import Trust
from golem.ranking.ranking import Ranking
from golem.resource.base.resourceserver import BaseResourceServer
from golem.resource.client import AsyncRequest, async_run
from golem.resource.dirmanager import DirManager, DirectoryType
from golem.resource.hyperdrive.resourcesmanager import HyperdriveResourceManager
from golem.rpc.mapping.aliases import Task, Network, Environment, UI, Payments
from golem.rpc.session import Publisher
from golem.task.taskbase import resource_types
from golem.task.taskserver import TaskServer
from golem.task.taskstate import TaskTestStatus
from golem.task.tasktester import TaskTester
from golem.tools import filelock
from golem.transactions.ethereum.ethereumtransactionsystem import \
    EthereumTransactionSystem

log = logging.getLogger("golem.client")


class ClientTaskComputerEventListener(object):
    def __init__(self, client):
        self.client = client

    def lock_config(self, on=True):
        self.client.lock_config(on)

    def config_changed(self):
        self.client.config_changed()


class Client(HardwarePresetsMixin):
    def __init__(self, datadir=None, transaction_system=False, connect_to_known_hosts=True,
                 use_docker_machine_manager=True, use_monitor=True, **config_overrides):

        if not datadir:
            datadir = get_local_datadir('default')

        self.datadir = datadir
        self.__lock_datadir()
        self.lock = Lock()
        self.task_tester = None

        # Read and validate configuration
        config = AppConfig.load_config(datadir)
        self.config_desc = ClientConfigDescriptor()
        self.config_desc.init_from_app_config(config)

        for key, val in config_overrides.iteritems():
            if not hasattr(self.config_desc, key):
                self.quit()  # quit only closes underlying services (for now)
                raise AttributeError(
                    "Can't override nonexistent config entry '{}'".format(key))
            setattr(self.config_desc, key, val)

        self.config_approver = ConfigApprover(self.config_desc)

        log.info('Client "%s", datadir: %s', self.config_desc.node_name, datadir)

        # Initialize database
        self.db = Database(datadir)

        # Hardware configuration
        HardwarePresets.initialize(self.datadir)
        HardwarePresets.update_config(self.config_desc.hardware_preset_name,
                                      self.config_desc)

        self.keys_auth = EllipticalKeysAuth(self.datadir)

        # NETWORK
        self.node = Node(node_name=self.config_desc.node_name,
                         prv_addr=self.config_desc.node_address,
                         key=self.keys_auth.get_key_id())

        self.p2pservice = None
        self.diag_service = None

        self.task_server = None
        self.last_nss_time = time.time()
        self.last_net_check_time = time.time()
        self.last_balance_time = time.time()
        self.last_tasks_time = time.time()

        self.last_node_state_snapshot = None

        self.nodes_manager_client = None

        self.do_work_task = task.LoopingCall(self.__do_work)
        self.publish_task = task.LoopingCall(self.__publish_events)

        self.cfg = config
        self.send_snapshot = False
        self.snapshot_lock = Lock()

        self.ranking = Ranking(self)

        if transaction_system:
            # Bootstrap transaction system if enabled.
            # TODO: Transaction system (and possible other modules) should be
            #       modeled as a Service that run independently.
            #       The Client/Application should be a collection of services.
            self.transaction_system = EthereumTransactionSystem(
                datadir, self.keys_auth._private_key)
        else:
            self.transaction_system = None

        self.use_docker_machine_manager = use_docker_machine_manager
        self.connect_to_known_hosts = connect_to_known_hosts
        self.environments_manager = EnvironmentsManager()
        self.daemon_manager = None

        self.rpc_publisher = None

        self.ipfs_manager = None
        self.resource_server = None
        self.resource_port = 0
        self.last_get_resource_peers_time = time.time()
        self.get_resource_peers_interval = 5.0
        self.use_monitor = use_monitor
        self.monitor = None
        self.session_id = uuid.uuid4().get_hex()
        dispatcher.connect(self.p2p_listener, signal='golem.p2p')
        dispatcher.connect(self.taskmanager_listener, signal='golem.taskmanager')

        atexit.register(self.quit)

    def configure_rpc(self, rpc_session):
        self.rpc_publisher = Publisher(rpc_session)

    def p2p_listener(self, sender, signal, event='default', **kwargs):
        if event != 'unreachable':
            return
        self.node.port_status = kwargs.get('description', u'')

    def taskmanager_listener(self, sender, signal, event='default', **kwargs):
        if event != 'task_status_updated':
            return
        self._publish(Task.evt_task_status, kwargs['task_id'])

    # TODO: re-enable
    def sync(self):
        pass

    def start(self):
        if self.use_monitor:
            self.init_monitor()
        try:
            self.start_network()
        except SystemExit:
            raise
        except Exception:
            log.critical('Can\'t start network. Giving up.', exc_info=True)
            sys.exit(1)

        self.do_work_task.start(1, False)
        self.publish_task.start(1, True)

    def start_network(self):
        log.info("Starting network ...")
        self.node.collect_network_info(self.config_desc.seed_host,
                                       use_ipv6=self.config_desc.use_ipv6)
        log.debug("Is super node? %s", self.node.is_super_node())

        # self.ipfs_manager = IPFSDaemonManager(connect_to_bootstrap_nodes=self.connect_to_known_hosts)
        # self.ipfs_manager.store_client_info()

        self.p2pservice = P2PService(self.node, self.config_desc, self.keys_auth,
                                     connect_to_known_hosts=self.connect_to_known_hosts)
        self.task_server = TaskServer(self.node, self.config_desc, self.keys_auth, self,
                                      use_ipv6=self.config_desc.use_ipv6,
                                      use_docker_machine_manager=self.use_docker_machine_manager)

        dir_manager = self.task_server.task_computer.dir_manager

        log.info("Starting resource server ...")
        self.daemon_manager = HyperdriveDaemonManager(self.datadir)
        hyperdrive_ports = self.daemon_manager.start()

        resource_manager = HyperdriveResourceManager(dir_manager)
        self.resource_server = BaseResourceServer(resource_manager, dir_manager,
                                                  self.keys_auth, self)

        def connect((p2p_port, task_port)):
            log.info('P2P server is listening on port %s', p2p_port)
            log.info('Task server is listening on port %s', task_port)

            dispatcher.send(signal='golem.p2p', event='listening',
                            port=[p2p_port, task_port] + list(hyperdrive_ports))

            listener = ClientTaskComputerEventListener(self)
            self.task_server.task_computer.register_listener(listener)
            self.p2pservice.connect_to_network()

            if self.monitor:
                self.diag_service.register(self.p2pservice,
                                           self.monitor.on_peer_snapshot)
                self.monitor.on_login()

        def terminate(*exceptions):
            log.error("Golem cannot listen on ports: %s", exceptions)
            self.quit()

        task = Deferred()
        p2p = Deferred()

        gatherResults([p2p, task], consumeErrors=True).addCallbacks(connect,
                                                                    terminate)
        log.info("Starting p2p server ...")
        self.p2pservice.task_server = self.task_server
        self.p2pservice.set_resource_server(self.resource_server)
        self.p2pservice.set_metadata_manager(self)
        self.p2pservice.start_accepting(listening_established=p2p.callback,
                                        listening_failure=p2p.errback)

        log.info("Starting task server ...")
        self.task_server.start_accepting(listening_established=task.callback,
                                         listening_failure=task.errback)

    def init_monitor(self):
        metadata = self.__get_nodemetadatamodel()
        self.monitor = SystemMonitor(metadata, MONITOR_CONFIG)
        self.monitor.start()
        self.diag_service = DiagnosticsService(DiagnosticsOutputFormat.data)
        self.diag_service.register(VMDiagnosticsProvider(), self.monitor.on_vm_snapshot)
        self.diag_service.start_looping_call()

    def connect(self, socket_address):
        if isinstance(socket_address, Iterable):
            socket_address = SocketAddress(socket_address[0], int(socket_address[1]))

        log.debug("P2pservice connecting to %s on port %s", socket_address.address, socket_address.port)
        self.p2pservice.connect(socket_address)

    def quit(self):
        if self.do_work_task.running:
            self.do_work_task.stop()
        if self.publish_task.running:
            self.publish_task.stop()
        if self.task_server:
            self.task_server.quit()
        if self.transaction_system:
            self.transaction_system.stop()
        if self.diag_service:
            self.diag_service.unregister_all()
        if self.daemon_manager:
            self.daemon_manager.stop()
        dispatcher.send(signal='golem.monitor', event='shutdown')
        if self.db:
            self.db.close()
        self._unlock_datadir()

    def key_changed(self):
        self.node.key = self.keys_auth.get_key_id()
        self.task_server.key_changed()
        self.p2pservice.key_changed()

    def stop_network(self):
        # FIXME: Implement this method properly - send disconnect package, close connections etc.
        self.p2pservice = None
        self.task_server = None
        self.nodes_manager_client = None

    def enqueue_new_task(self, t_dict):
        task = self.task_server.task_manager.create_task(t_dict)
        task_id = task.header.task_id
        files = task.get_resources(None, resource_types["hashes"])
        client_options = self.resource_server.resource_manager.build_client_options(self.keys_auth.key_id)
        deferred = self.resource_server.add_task(files, task_id, client_options=client_options)
        deferred.addCallback(lambda _: self.task_server.task_manager.add_new_task(task))
        return task

    def task_resource_send(self, task_id):
        self.task_server.task_manager.resources_send(task_id)

    def task_resource_collected(self, task_id, unpack_delta=True):
        self.task_server.task_computer.task_resource_collected(task_id, unpack_delta)

    def task_resource_failure(self, task_id, reason):
        self.task_server.task_computer.task_resource_failure(task_id, reason)

    def set_resource_port(self, resource_port):
        self.resource_port = resource_port
        self.p2pservice.set_resource_peer(self.node.prv_addr, self.resource_port)

    def run_test_task(self, t_dict):
        if self.task_tester is None:
            request = AsyncRequest(self._run_test_task, t_dict)
            async_run(request)
            return True

        if self.rpc_publisher:
            self.rpc_publisher.publish(Task.evt_task_test_status, TaskTestStatus.error,
                                       u"Another test is running")
        return False

    def _run_test_task(self, t_dict):

        def on_success(*args, **kwargs):
            self.task_tester = None
            self._publish(Task.evt_task_test_status,
                          TaskTestStatus.success, *args, **kwargs)

        def on_error(*args, **kwargs):
            self.task_tester = None
            self._publish(Task.evt_task_test_status,
                          TaskTestStatus.error, *args, **kwargs)

        t = DictSerializer.load(t_dict)
        self.task_tester = TaskTester(t, self.datadir, on_success, on_error)
        self.task_tester.run()
        self._publish(Task.evt_task_test_status, TaskTestStatus.started, True)

    def abort_test_task(self):
        with self.lock:
            if self.task_tester is not None:
                self.task_tester.end_comp()
                return True
            return False

    def create_task(self, t_dict):
        try:
            new_task = self.enqueue_new_task(t_dict)
            return unicode(new_task.header.task_id)
        except Exception:
            log.exception("Cannot create task {}".format(t_dict))

    def abort_task(self, task_id):
        self.task_server.task_manager.abort_task(task_id)

    def restart_task(self, task_id):
        self.task_server.task_manager.restart_task(task_id)

    def restart_subtask(self, subtask_id):
        self.task_server.task_manager.restart_subtask(subtask_id)

    def pause_task(self, task_id):
        self.task_server.task_manager.pause_task(task_id)

    def resume_task(self, task_id):
        self.task_server.task_manager.resume_task(task_id)

    def delete_task(self, task_id):
        self.remove_task_header(task_id)
        self.remove_task(task_id)
        self.task_server.task_manager.delete_task(task_id)

    def get_node(self):
        return DictSerializer.dump(self.node)

    def get_node_name(self):
        name = self.config_desc.node_name
        return unicode(name) if name else u''

    def get_neighbours_degree(self):
        return self.p2pservice.get_peers_degree()

    def get_suggested_addr(self, key_id):
        return self.p2pservice.suggested_address.get(key_id)

    def get_suggested_conn_reverse(self, key_id):
        return self.p2pservice.get_suggested_conn_reverse(key_id)

    def get_resource_peers(self):
        self.p2pservice.send_get_resource_peers()

    def get_peers(self):
        return self.p2pservice.peers.values()

    def get_known_peers(self):
        peers = self.p2pservice.free_peers or []
        return [DictSerializer.dump(PeerSessionInfo(p), typed=False) for p in peers]

    def get_connected_peers(self):
        peers = self.get_peers() or []
        return [DictSerializer.dump(PeerSessionInfo(p), typed=False) for p in peers]

    def get_public_key(self):
        return self.keys_auth.public_key

    def get_dir_manager(self):
        if self.task_server:
            return self.task_server.task_computer.dir_manager

    def load_keys_from_file(self, file_name):
        if file_name != "":
            return self.keys_auth.load_from_file(file_name)
        return False

    def save_keys_to_files(self, private_key_path, public_key_path):
        return self.keys_auth.save_to_files(private_key_path, public_key_path)

    def get_key_id(self):
        return self.get_client_id()

    def get_difficulty(self):
        return self.keys_auth.get_difficulty()

    def get_client_id(self):
        key_id = self.keys_auth.get_key_id()
        return unicode(key_id) if key_id else None

    def get_node_key(self):
        key = self.node.key
        return unicode(key) if key else None

    def get_settings(self):
        return DictSerializer.dump(self.config_desc)

    def get_setting(self, key):
        if not hasattr(self.config_desc, key):
            raise KeyError(u"Unknown setting: {}".format(key))

        value = getattr(self.config_desc, key)
        if key in ConfigApprover.numeric_opt:
            return unicode(value)
        return value

    def update_setting(self, key, value):
        if not hasattr(self.config_desc, key):
            raise KeyError(u"Unknown setting: {}".format(key))
        setattr(self.config_desc, key, value)
        self.change_config(self.config_desc)

    def update_settings(self, settings_dict, run_benchmarks=False):
        cfg_desc = DictSerializer.load(settings_dict)
        self.change_config(cfg_desc, run_benchmarks)

    def get_datadir(self):
        return unicode(self.datadir)

    def get_p2p_port(self):
        return self.p2pservice.cur_port

    def get_task_server_port(self):
        return self.task_server.cur_port

    def get_task_count(self):
        return len(self.task_server.task_keeper.get_all_tasks())

    def get_task(self, task_id):
        return self.task_server.task_manager.get_dict_task(task_id)

    def get_tasks(self, task_id=None):
        if task_id:
            return self.task_server.task_manager.get_dict_task(task_id)
        return self.task_server.task_manager.get_dict_tasks()

    def get_subtasks(self, task_id):
        return self.task_server.task_manager.get_dict_subtasks(task_id)

    def get_subtask(self, subtask_id):
        return self.task_server.task_manager.get_dict_subtask(subtask_id)

    def get_task_stats(self):
        return {
            u'in_network': self.get_task_count(),
            u'supported': self.get_supported_task_count(),
            u'subtasks_computed': self.get_computed_task_count(),
            u'subtasks_with_errors': self.get_error_task_count(),
            u'subtasks_with_timeout': self.get_timeout_task_count()
        }

    def get_supported_task_count(self):
        return len(self.task_server.task_keeper.supported_tasks)

    def get_computed_task_count(self):
        return self.task_server.task_computer.stats.get_stats('computed_tasks')

    def get_timeout_task_count(self):
        return self.task_server.task_computer.stats.get_stats('tasks_with_timeout')

    def get_error_task_count(self):
        return self.task_server.task_computer.stats.get_stats('tasks_with_errors')

    def get_payment_address(self):
        address = self.transaction_system.get_payment_address()
        return unicode(address) if address else None

    @inlineCallbacks
    def get_balance(self):
        if self.use_transaction_system():
            req = AsyncRequest(self.transaction_system.get_balance)
            b, ab, d = yield async_run(req)
            if b is not None:
                returnValue((unicode(b), unicode(ab), unicode(d)))
        returnValue((None, None, None))

    def get_payments_list(self):
        if self.use_transaction_system():
            payments = self.transaction_system.get_payments_list()
            return map(self._map_payment, payments)
        return ()

    def get_incomes_list(self):
        # Will be implemented in incomes_core
        return []

    @classmethod
    def _map_payment(cls, obj):
        obj["payee"] = to_unicode(obj["payee"])
        obj["value"] = to_unicode(obj["value"])
        obj["fee"] = to_unicode(obj["fee"])
        return obj

    def get_task_cost(self, task_id):
        """
        Get current cost of the task defined by @task_id
        :param task_id: Task ID
        :return: Cost of the task
        """
        cost = self.task_server.task_manager.get_payment_for_task_id(task_id)
        if cost is None:
            return 0.0
        return cost

    def use_transaction_system(self):
        return bool(self.transaction_system)

    def get_computing_trust(self, node_id):
        if self.use_ranking():
            return self.ranking.get_computing_trust(node_id)
        return None

    def get_requesting_trust(self, node_id):
        if self.use_ranking():
            return self.ranking.get_requesting_trust(node_id)
        return None

    def get_description(self):
        try:
            account, _ = Account.get_or_create(node_id=self.get_client_id())
            return account.description
        except Exception as e:
            return u"An error has occurred {}".format(e)

    def change_description(self, description):
        self.get_description()
        q = Account.update(description=description).where(Account.node_id == self.get_client_id())
        q.execute()

    def use_ranking(self):
        return bool(self.ranking)

    def want_to_start_task_session(self, key_id, node_id, conn_id):
        self.p2pservice.want_to_start_task_session(key_id, node_id, conn_id)

    def inform_about_task_nat_hole(self, key_id, rv_key_id, addr, port, ans_conn_id):
        self.p2pservice.inform_about_task_nat_hole(key_id, rv_key_id, addr, port, ans_conn_id)

    def inform_about_nat_traverse_failure(self, key_id, res_key_id, conn_id):
        self.p2pservice.inform_about_nat_traverse_failure(key_id, res_key_id, conn_id)

    # CLIENT CONFIGURATION
    def set_rpc_server(self, rpc_server):
        self.rpc_server = rpc_server
        return self.rpc_server.add_service(self)

    def change_config(self, new_config_desc, run_benchmarks=False):
        self.config_desc = self.config_approver.change_config(new_config_desc)
        self.cfg.change_config(self.config_desc)
        self.p2pservice.change_config(self.config_desc)
        self.upsert_hw_preset(HardwarePresets.from_config(self.config_desc))
        if self.task_server:
            self.task_server.change_config(self.config_desc, run_benchmarks=run_benchmarks)
        dispatcher.send(signal='golem.monitor', event='config_update', meta_data=self.__get_nodemetadatamodel())

    def register_nodes_manager_client(self, nodes_manager_client):
        self.nodes_manager_client = nodes_manager_client

    def change_timeouts(self, task_id, full_task_timeout, subtask_timeout):
        self.task_server.change_timeouts(task_id, full_task_timeout, subtask_timeout)

    def query_task_state(self, task_id):
        state = self.task_server.task_manager.query_task_state(task_id)
        if state:
            return DictSerializer.dump(state)

    def pull_resources(self, task_id, resources, client_options=None):
        self.resource_server.download_resources(resources, task_id, client_options=client_options)

    def add_resource_peer(self, node_name, addr, port, key_id, node_info):
        self.resource_server.add_resource_peer(node_name, addr, port, key_id, node_info)

    def get_res_dirs(self):
        return {u"computing": self.get_computed_files_dir(),
                u"received": self.get_received_files_dir(),
                u"distributed": self.get_distributed_files_dir()}

    def get_res_dirs_sizes(self):
        return {unicode(name): unicode(du(d))
                for name, d in self.get_res_dirs().iteritems()}

    def get_res_dir(self, dir_type):
        if dir_type == DirectoryType.COMPUTED:
            return self.get_computed_files_dir()
        elif dir_type == DirectoryType.DISTRIBUTED:
            return self.get_distributed_files_dir()
        elif dir_type == DirectoryType.RECEIVED:
            return self.get_received_files_dir()
        raise Exception(u"Unknown dir type: {}".format(dir_type))

    def get_computed_files_dir(self):
        return unicode(self.task_server.get_task_computer_root())

    def get_received_files_dir(self):
        return unicode(self.task_server.task_manager.get_task_manager_root())

    def get_distributed_files_dir(self):
        return unicode(self.resource_server.get_distributed_resource_root())

    def clear_dir(self, dir_type):
        if dir_type == DirectoryType.COMPUTED:
            return self.remove_computed_files()
        elif dir_type == DirectoryType.DISTRIBUTED:
            return self.remove_distributed_files()
        elif dir_type == DirectoryType.RECEIVED:
            return self.remove_received_files()
        raise Exception(u"Unknown dir type: {}".format(dir_type))

    def remove_computed_files(self):
        dir_manager = DirManager(self.datadir)
        dir_manager.clear_dir(self.get_computed_files_dir())

    def remove_distributed_files(self):
        dir_manager = DirManager(self.datadir)
        dir_manager.clear_dir(self.get_distributed_files_dir())

    def remove_received_files(self):
        dir_manager = DirManager(self.datadir)
        dir_manager.clear_dir(self.get_received_files_dir())

    def remove_task(self, task_id):
        self.p2pservice.remove_task(task_id)

    def remove_task_header(self, task_id):
        self.task_server.remove_task_header(task_id)

    def get_known_tasks(self):
        headers = {}
        for key, header in self.task_server.task_keeper.task_headers.iteritems():
            headers[unicode(key)] = DictSerializer.dump(header)
        return headers

    def get_environments(self):
        envs = copy(self.environments_manager.get_environments())
        return [{
            u'id': unicode(env.get_id()),
            u'supported': env.supported(),
            u'accepted': env.is_accepted(),
            u'performance': env.get_performance(self.config_desc),
            u'description': unicode(env.short_description)
        } for env in envs]

    @inlineCallbacks
    def run_benchmark(self, env_id):
        # TODO: move benchmarks to environments
        from apps.blender.blenderenvironment import BlenderEnvironment
        from apps.lux.luxenvironment import LuxRenderEnvironment

        deferred = Deferred()

        if env_id == BlenderEnvironment.get_id():
            self.task_server.task_computer.run_blender_benchmark(
                deferred.callback, deferred.errback
            )
        elif env_id == LuxRenderEnvironment.get_id():
            self.task_server.task_computer.run_lux_benchmark(
                deferred.callback, deferred.errback
            )
        else:
            raise Exception("Unknown environment: {}".format(env_id))

        result = yield deferred
        returnValue(result)

    def enable_environment(self, env_id):
        self.environments_manager.change_accept_tasks(env_id, True)

    def disable_environment(self, env_id):
        self.environments_manager.change_accept_tasks(env_id, False)

    def send_gossip(self, gossip, send_to):
        return self.p2pservice.send_gossip(gossip, send_to)

    def send_stop_gossip(self):
        return self.p2pservice.send_stop_gossip()

    def collect_gossip(self):
        return self.p2pservice.pop_gossips()

    def collect_stopped_peers(self):
        return self.p2pservice.pop_stop_gossip_form_peers()

    def collect_neighbours_loc_ranks(self):
        return self.p2pservice.pop_neighbours_loc_ranks()

    def push_local_rank(self, node_id, loc_rank):
        self.p2pservice.push_local_rank(node_id, loc_rank)

    def check_payments(self):
        if not self.transaction_system:
            return
        after_deadline_nodes = self.transaction_system.check_payments()
        for node_id in after_deadline_nodes:
            Trust.PAYMENT.decrease(node_id)

    def _publish(self, event_name, *args, **kwargs):
        if self.rpc_publisher:
            self.rpc_publisher.publish(event_name, *args, **kwargs)

    def lock_config(self, on=True):
        self._publish(UI.evt_lock_config, on)

    def config_changed(self):
        self._publish(Environment.evt_opts_changed)

    def __get_nodemetadatamodel(self):
        return NodeMetadataModel(self.get_client_id(), self.session_id, sys.platform,
                                 APP_VERSION, self.get_description(), self.config_desc)

    def __try_to_change_to_number(self, old_value, new_value, to_int=False, to_float=False, name="Config"):
        try:
            if to_int:
                new_value = int(new_value)
            elif to_float:
                new_value = float(new_value)
        except ValueError:
            log.warning("%s value '%s' is not a number", name, new_value)
            new_value = old_value
        return new_value

    def __do_work(self):
        if not self.p2pservice:
            return

        if self.config_desc.send_pings:
            self.p2pservice.ping_peers(self.config_desc.pings_interval)

        try:
            self.p2pservice.sync_network()
        except Exception:
            log.exception("p2pservice.sync_network failed")
        try:
            self.task_server.sync_network()
        except Exception:
            log.exception("task_server.sync_network failed")
        try:
            self.resource_server.sync_network()
        except Exception:
            log.exception("resource_server.sync_network failed")
        try:
            self.ranking.sync_network()
        except Exception:
            log.exception("ranking.sync_network failed")
        try:
            self.check_payments()
        except Exception:
            log.exception("check_payments failed")

    @inlineCallbacks
    def __publish_events(self):
        now = time.time()

        if now - self.last_nss_time > max(self.config_desc.node_snapshot_interval, 1):
            dispatcher.send(
                signal='golem.monitor',
                event='stats_snapshot',
                known_tasks=self.get_task_count(),
                supported_tasks=self.get_supported_task_count(),
                stats=self.task_server.task_computer.stats,
            )
            dispatcher.send(
                signal='golem.monitor',
                event='task_computer_snapshot',
                task_computer=self.task_server.task_computer,
            )
            # with self.snapshot_lock:
            #     self.__make_node_state_snapshot()
            #     self.manager_server.sendStateMessage(self.last_node_state_snapshot)
            self.last_nss_time = time.time()

        if now - self.last_net_check_time >= self.config_desc.network_check_interval:
            self.last_net_check_time = time.time()
            self._publish(Network.evt_connection, self.connection_status())

        if now - self.last_tasks_time >= PUBLISH_TASKS_INTERVAL:
            self._publish(Task.evt_task_list, self.get_tasks())

        if now - self.last_balance_time >= PUBLISH_BALANCE_INTERVAL:
            try:
                gnt, av_gnt, eth = yield self.get_balance()
            except Exception as exc:
                log.debug('Error retrieving balance: {}'.format(exc))
            else:
                self._publish(Payments.evt_balance, {
                    u'GNT': unicode(gnt),
                    u'GNT_available': unicode(av_gnt),
                    u'ETH': unicode(eth)
                })

    def __make_node_state_snapshot(self, is_running=True):
        peers_num = len(self.p2pservice.peers)
        last_network_messages = self.p2pservice.get_last_messages()

        if self.task_server:
            tasks_num = len(self.task_server.task_keeper.task_headers)
            remote_tasks_progresses = self.task_server.task_computer.get_progresses()
            local_tasks_progresses = self.task_server.task_manager.get_progresses()
            last_task_messages = self.task_server.get_last_messages()
            self.last_node_state_snapshot = NodeStateSnapshot(is_running,
                                                              self.config_desc.node_name,
                                                              peers_num,
                                                              tasks_num,
                                                              self.p2pservice.node.pub_addr,
                                                              self.p2pservice.node.pub_port,
                                                              last_network_messages,
                                                              last_task_messages,
                                                              remote_tasks_progresses,
                                                              local_tasks_progresses)
        else:
            self.last_node_state_snapshot = NodeStateSnapshot(self.config_desc.node_name, peers_num)

        if self.nodes_manager_client:
            self.nodes_manager_client.send_client_state_snapshot(self.last_node_state_snapshot)

    def connection_status(self):
        listen_port = self.get_p2p_port()
        task_server_port = self.get_task_server_port()

        if listen_port == 0 or task_server_port == 0:
            return u"Application not listening, check config file."

        messages = []

        if self.node.port_status:
            statuses = self.node.port_status.split('\n')
            failures = filter(lambda e: e.find('open') == -1, statuses)
            messages.append(u"Port " + u", ".join(failures) + u".")

        if self.get_connected_peers():
            messages.append(u"Connected")
        else:
            messages.append(u"Not connected to Golem Network, "
                            u"check seed parameters.")

        return u' '.join(messages)

    def get_metadata(self):
        metadata = dict()
        # if self.ipfs_manager:
        #     metadata.update(self.ipfs_manager.get_metadata())
        return metadata

    def interpret_metadata(self, metadata, address, port, node_info):
        pass
        # if self.config_desc and node_info and metadata:
        #     seed_addresses = self.p2pservice.get_seeds()
        #     node_addresses = [
        #         (address, port),
        #         (node_info.pub_addr, node_info.pub_port)
        #     ]
        #     self.ipfs_manager.interpret_metadata(metadata,
        #                                          seed_addresses,
        #                                          node_addresses)

    def get_status(self):
        progress = self.task_server.task_computer.get_progresses()
        if len(progress) > 0:
            msg = u"Computing {} subtask(s):".format(len(progress))
            for k, v in progress.iteritems():
                msg = u"{} \n {} ({}%)\n".format(msg, k, v.get_progress() * 100)
        elif self.config_desc.accept_tasks:
            msg = u"Waiting for tasks...\n"
        else:
            msg = u"Not accepting tasks\n"

        peers = self.p2pservice.get_peers()

        msg += u"Active peers in network: {}\n".format(len(peers))
        return msg

    def activate_hw_preset(self, name, run_benchmarks=False):
        HardwarePresets.update_config(name, self.config_desc)
        if hasattr(self, 'task_server') and self.task_server:
            self.task_server.change_config(self.config_desc, run_benchmarks=run_benchmarks)

    def __lock_datadir(self):
        if not path.exists(self.datadir):
            # Create datadir if not exists yet.
            # TODO: It looks we have the same code in many places
            makedirs(self.datadir)
        self.__datadir_lock = open(path.join(self.datadir, "LOCK"), 'w')
        flags = filelock.LOCK_EX | filelock.LOCK_NB
        try:
            filelock.lock(self.__datadir_lock, flags)
        except IOError:
            raise IOError("Data dir {} used by other Golem instance"
                          .format(self.datadir))

    def _unlock_datadir(self):
        # solves locking issues on OS X
        try:
            filelock.unlock(self.__datadir_lock)
        except Exception:
            pass
        self.__datadir_lock.close()
