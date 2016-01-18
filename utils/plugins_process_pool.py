import random
from multiprocessing import JoinableQueue

from utils.worker_process import WorkerProcess


class PluginsProcessPool(object):
    """Creates a pool of processes and dispatches scanning commands to be run."""

    DEFAULT_MAX_PROCESSES_NB = 12
    DEFAULT_PROCESSES_PER_HOSTNAME_NB = 3

    def __init__(self, available_plugins, network_retries, network_timeout, max_processes_nb=DEFAULT_MAX_PROCESSES_NB,
                 max_processes_per_hostname_nb=DEFAULT_PROCESSES_PER_HOSTNAME_NB):
        self._available_plugins = available_plugins
        self._network_retries = network_retries
        self._network_timeout = network_timeout
        self._max_processes_nb = max_processes_nb
        self._max_processes_per_hostname_nb = max_processes_per_hostname_nb

        # Create hostname-specific queues to ensure aggressive scan commands targeting this hostname are never
        # run concurrently
        self._hostname_queues_dict = {}
        self._processes_dict = {}

        self._task_queue = JoinableQueue()  # Processes get tasks from task_queue and
        self._result_queue = JoinableQueue()  # put the result of each task in result_queue
        self._queued_tasks_nb = 0


    def queue_plugin_task(self, server_connectivity_info, plugin_command, plugin_options_dict):
        # Ensure we have the right processes and queues in place for this hostname
        self._check_and_create_process(server_connectivity_info.hostname)

        # Add the task to the right queue
        self._queued_tasks_nb += 1
        if plugin_command in self._available_plugins.get_aggressive_commands():
            # Aggressive commands should not be run in parallel against
            # a given server so we use the priority queues to prevent this
            self._hostname_queues_dict[server_connectivity_info.hostname].put((server_connectivity_info, plugin_command,
                                                                               plugin_options_dict))
        else:
            # Normal commands get put in the standard/shared queue
            self._task_queue.put((server_connectivity_info, plugin_command, plugin_options_dict))


    def _check_and_create_process(self, hostname):
        if hostname not in self._hostname_queues_dict.keys():
            # We haven't this hostname before
            if self._get_current_processes_nb() < self._max_processes_nb:
                # Create a new process and new queue for this hostname
                hostname_queue = JoinableQueue()
                self._hostname_queues_dict[hostname] = hostname_queue

                process = WorkerProcess(hostname_queue, self._task_queue, self._result_queue,
                                        self._available_plugins.get_commands(), self._network_retries,
                                        self._network_timeout)
                process.start()
                self._processes_dict[hostname] = [process]
            else:
                # We are already using the maximum number of processes
                # Do not create a process and re-use a random existing hostname queue
                self._hostname_queues_dict[hostname] = random.choice(self._hostname_queues_dict.values())
                self._processes_dict[hostname] = []

        else:
            # We have seen this hostname before - create a new process if possible
            if len(self._processes_dict[hostname]) < self._max_processes_per_hostname_nb \
                    and self._get_current_processes_nb() < self._max_processes_nb:
                # We can create a new process; no need to create a queue as it already exists
                process = WorkerProcess(self._hostname_queues_dict[hostname], self._task_queue, self._result_queue,
                                        self._available_plugins.get_commands(), self._network_retries,
                                        self._network_timeout)
                process.start()
                self._processes_dict[hostname].append(process)


    def _get_current_processes_nb(self):
        return sum([len(process_list) for hostname, process_list in self._processes_dict.iteritems()])


    def get_results(self):
        """New tasks cannot be queued once this is called. Returns a list of tuples of
        (server_info, plugin_command, plugin_result)."""
        # Put a 'None' sentinel in the queue to let the each process know when every task has been completed
        for _ in xrange(self._get_current_processes_nb()):
            self._task_queue.put(None)

        for hostname, hostname_queue in self._hostname_queues_dict.iteritems():
            for i in xrange(len(self._processes_dict[hostname])):
                hostname_queue.put(None)

        received_task_results = 0
        # Go on until all the tasks have been completed and all processes are done
        expected_task_results = self._queued_tasks_nb + self._get_current_processes_nb()
        while received_task_results != expected_task_results:
            result = self._result_queue.get()
            self._result_queue.task_done()
            received_task_results += 1
            if result is None:
                # Getting None means that one process was done
                pass
            else:
                # Getting an actual result
                yield result

        # Ensure all the queues and processes are done
        self._task_queue.join()
        self._result_queue.join()
        for hostname_queue in self._hostname_queues_dict.values():
            hostname_queue.join()
        for process_list in self._processes_dict.values():
            [process.join() for process in process_list]  # Causes interpreter shutdown errors


    def emergency_shutdown(self):
        # Terminating a process this way will corrupt the queues but we're shutting down anyway
        for process_list in self._processes_dict.values():
            [process.terminate() for process in process_list]