"""
Module that manages webserver and tank
"""
import os.path
import os
import signal
import multiprocessing
import logging
import logging.handlers
import traceback
import json
import time

import yandex_tank_api.common
import yandex_tank_api.worker
import yandex_tank_api.webserver


logger = logging.getLogger(__name__)


class TankRunner(object):
    """
    Manages the tank process and its working directory.
    """

    def __init__(
            self, cfg, manager_queue, session_id, tank_config, first_break):
        """
        Sets up working directory and tank queue
        Starts tank process
        """

        work_dir = os.path.join(cfg['tests_dir'], session_id)
        lock_dir = cfg['lock_dir']
        load_ini_path = os.path.join(work_dir, 'load.ini')

        # Create load.ini
        logger.info("Saving tank config to %s", load_ini_path)
        tank_config_file = open(load_ini_path, 'w')
        tank_config_file.write(tank_config)

        # Create tank queue and put first break there
        self.tank_queue = multiprocessing.Queue()
        self.set_break(first_break)

        ignore_machine_defaults = cfg['ignore_machine_defaults']

        # Start tank process
        self.tank_process = multiprocessing.Process(
            target=yandex_tank_api.worker.run,
            args=(
                self.tank_queue, manager_queue, work_dir, lock_dir, session_id,
                ignore_machine_defaults))
        self.tank_process.start()

    def set_break(self, next_break):
        """Sends the next break to the tank process"""
        self.tank_queue.put({'break': next_break})

    def is_alive(self):
        """Check that the tank process didn't exit """
        return self.tank_process.exitcode is None

    def get_exitcode(self):
        """Return tank exitcode"""
        return self.tank_process.exitcode

    def join(self):
        """Joins the tank process"""
        logger.info("Waiting for tank exit...")
        return self.tank_process.join()

    def stop(self, remove_break):
        """Interrupts the tank process"""
        if self.is_alive():
            sig = signal.SIGTERM if remove_break else signal.SIGINT
            os.kill(self.tank_process.pid, sig)

    def __del__(self):
        self.stop(remove_break=True)


class Manager(object):
    """
    Implements the message processing logic
    """

    def __init__(self, cfg):
        """Sets up initial state of Manager"""

        self.cfg = cfg

        self.manager_queue = multiprocessing.Queue()
        self.webserver_queue = multiprocessing.Queue()

        self.webserver_process = multiprocessing.Process(
            target=yandex_tank_api.webserver.main,
            args=(
                self.webserver_queue, self.manager_queue, cfg['tests_dir'],
                cfg['tornado_debug']))
        self.webserver_process.daemon = True
        self.webserver_process.start()

        self._reset_session()

    def _reset_session(self):
        """
        Resets session state variables
        Should be called only when tank is not running
        """
        logger.info("Resetting current session variables")
        self.session_id = None
        self.tank_runner = None
        self.last_tank_status = 'not started'

    def _handle_cmd_stop(self, msg):
        """Check running session and kill tank"""
        if msg['session'] == self.session_id:
            self.tank_runner.stop(remove_break=False)
        else:
            logger.error("Can stop only current session")

    def _handle_cmd_set_break(self, msg):
        """New break for running session"""
        if msg['session'] != self.session_id:
            raise RuntimeError(
                "Webserver requested to start session "
                "when another one is already running")
        elif 'break' in msg:
            self.tank_runner.set_break(msg['break'])
        else:
            # Internal protocol error
            logger.error(
                "Recieved run command without break:\n%s", json.dumps(msg))

    def _handle_cmd_new_session(self, msg):
        """Start new session"""
        if 'session' not in msg or 'config' not in msg:
            # Internal protocol error
            logger.critical(
                "Not enough data to start new session: "
                "both config and test should be present:%s\n", json.dumps(msg))
            return
        try:
            self.tank_runner = TankRunner(
                cfg=self.cfg,
                manager_queue=self.manager_queue,
                session_id=msg['session'],
                tank_config=msg['config'],
                first_break=msg['break'])
        except KeyboardInterrupt:
            pass
        except Exception as ex:
            self.webserver_queue.put({
                'session': msg['session'],
                'status': 'failed',
                'break': msg['break'],
                'reason': 'Failed to start tank:\n' + traceback.format_exc(ex)
            })
        else:
            self.session_id = msg['session']

    def _handle_cmd(self, msg):
        """Process command from webserver"""

        if 'session' not in msg:
            logger.error("Bad command: session id not specified")
            return

        cmd = msg['cmd']

        if cmd == 'stop':
            self._handle_cmd_stop(msg)
        elif cmd == 'run':
            if self.session_id is not None:
                self._handle_cmd_set_break(msg)
            else:
                self._handle_cmd_new_session(msg)
        else:
            logger.critical("Unknown command: %s", cmd)

    def _handle_tank_exit(self):
        """
        Empty manager queue.
        Report if tank died unexpectedly.
        Reset session.
        """
        logging.info("Tank exit, sleeping 1 s and handling remaining messages")
        time.sleep(1)
        while True:
            try:
                msg = self.manager_queue.get(block=False)
            except multiprocessing.queues.Empty:
                break
            self._handle_msg(msg)
        if self.last_tank_status == 'running'\
                or not self.tank_runner\
                or self.tank_runner.get_exitcode() != 0:
            # Report unexpected death
            self.webserver_queue.put({
                'session': self.session_id,
                'status': 'failed',
                'reason': "Tank died unexpectedly. Last reported "
                "status: % s, worker exitcode: % s" % (
                    self.last_tank_status,
                    self.tank_runner.get_exitcode() if self.tank_runner else None)
            })
        # In any case, reset the session
        self._reset_session()

    def _handle_webserver_exit(self):
        """Stop tank and raise RuntimeError"""
        logger.error("Webserver died unexpectedly.")
        if self.tank_runner is not None:
            logger.warning("Stopping tank...")
            self.tank_runner.stop(remove_break=True)
            self.tank_runner.join()
        raise RuntimeError("Unexpected webserver exit")

    def run(self):
        """
        Manager event loop.
        Process message from self.manager_queue
        Check that tank is alive.
        Check that webserver is alive.
        """

        while True:
            if self.session_id is not None and not self.tank_runner.is_alive():
                self._handle_tank_exit()
            if not self.webserver_process.is_alive():
                self._handle_webserver_exit()
            try:
                msg = self.manager_queue.get(
                    block=True, timeout=self.cfg['message_check_interval'])
            except multiprocessing.queues.Empty:
                continue
            self._handle_msg(msg)

    def _handle_msg(self, msg):
        """Handle message from manager queue"""
        logger.info("Recieved message:\n%s", json.dumps(msg))
        if 'cmd' in msg:
            # Recieved command from server
            self._handle_cmd(msg)
        elif 'status' in msg:
            # This is a status message from tank
            self._handle_tank_status(msg)
        else:
            logger.error("Strange message (not a command and not a status) ")

    def _handle_tank_status(self, msg):
        """
        Wait for tank exit if it stopped.
        Remember new status and notify webserver.
        """
        new_status = msg['status']

        if self.last_tank_status not in ['success', 'failed'] \
                and new_status in ['success', 'failed']:
            self.tank_runner.join()
            self._reset_session()

        self.last_tank_status = msg['status']

        self.webserver_queue.put(msg)


def run_server(options):
    """Runs the whole yandex-tank-api server """

    # Configure
    # TODO: un-hardcode cfg
    cfg = {
        'message_check_interval': 1.0,
        'tests_dir': options.work_dir + '/tests',
        'ignore_machine_defaults': options.ignore_machine_defaults,
        'tornado_debug': options.debug,
        'lock_dir': options.lock_dir,
    }

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    if options.log_file is None:
        handler = logging.StreamHandler()
    else:
        handler = logging.handlers.RotatingFileHandler(
            options.log_file, maxBytes=1000000, backupCount=16)

    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
    root_logger.addHandler(handler)

    logger = logging.getLogger(__name__)
    try:
        logger.info("Starting server")
        Manager(cfg).run()
    except KeyboardInterrupt:
        logger.info("Interrupted, terminating")
    except Exception:
        logger.exception("Unhandled exception in manager.run_server:")
    except:
        logger.error("Caught something strange in manager.run_server")
