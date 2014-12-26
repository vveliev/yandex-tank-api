"""
Tank worker process for yandex-tank-api
Based on ConsoleWorker from yandex-tank
"""

import fnmatch
import logging
import os
import os.path
import sys
import traceback
import signal
import json

#Yandex.Tank modules
#TODO: split yandex-tank and make python-way install
sys.path.append('/usr/lib/yandex-tank')
import tankcore

#Yandex.Tank.Api modules

#Test stage order, internal protocol description, etc...
import common


def signal_handler(sig, frame):
    """ Converts SIGTERM and SIGINT into KeyboardInterrupt() exception """
    raise KeyboardInterrupt()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

class TankWorker:
    """    Worker class that runs tank core until the next breakpoint   """

    IGNORE_LOCKS = "ignore_locks"

    def __init__(self, tank_queue, manager_queue, working_dir):
        
        #Parameters from manager
        self.tank_queue=tank_queue
        self.manager_queue=manager_queue
        self.working_dir = working_dir

        #State variables
        self.break_at='lock'
        self.stage='started' #Not reported anywhere to anybody
        self.failures=[]

        self.log = logging.getLogger(__name__)
        self.core = tankcore.TankCore()

    def __add_log_file(self,logger,loglevel,filename):
        """Adds FileHandler to logger; adds filename to artifacts"""
        full_filename=os.path.join(self.working_dir,filename)

        self.core.add_artifact_file(full_filename)

        handler = logging.FileHandler(full_filename)
        handler.setLevel(loglevel)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
        logger.addHandler(handler)

    def __setup_logging(self):
        """
        Logging setup.
        Should be called only after the lock is acquired.
        """
        logger = logging.getLogger('')
        logger.setLevel(logging.DEBUG)

        self.__add_log_file(logger,logging.DEBUG,'tank.log')
        self.__add_log_file(logger,logging.INFO,'tank_brief.log')

    def __get_configs_from_dir(self,config_dir):
        """ 
        Returns configs from specified directory, sorted alphabetically
        """
        configs = []
        try:
            conf_files = os.listdir(config_dir)
            conf_files.sort()
            for filename in conf_files:
                if fnmatch.fnmatch(filename, '*.ini'):
                    config_file = os.path.realpath(config_dir + os.sep + filename)
                    self.log.debug("Adding config file: %s",config_file)
                    configs += [config_file]
        except OSError:
            self.log.error("Failed to get configs from %s",config_dir)

        return configs

    def __get_configs(self):
        """Returns list of all configs for this test"""
        configs=[]
        for cfg_dir in  ['/etc/yandex-tank/', 
                         '/etc/yandex-tank-api/defaults',
                          self.working_dir,
                          '/etc/yandex-tank-api/override']:
            configs += self.__get_configs_from_dir(cfg_dir)
        return configs


    def __preconfigure(self):
        """Logging and TankCore setup"""
        self.__setup_logging()
        self.core.load_configs( self.__get_configs() )
        self.core.load_plugins()

    def get_next_break(self):
        """
        Read the next break from tank queue
        Check it for sanity
        """
        while True:
            msg=self.tank_queue.get()
            #Check that there is a break in the message
            if 'break' not in msg:
                self.log.error("No break sepcified in the recieved message from manager")
                continue
            br=msg['break']
            #Check taht the name is valid
            if br not in common.test_stage_order:
                self.log.error("Manager requested break at an unknown stage: %s",br)
            #Check that the break is later than br
            elif common.is_A_earlier_than_B(br,self.break_at):
                self.log.error("Recieved break %s which is earlier than current next break %s",br,self.break_at)
            else:
                self.log.info("Changing the next break from %s to %s",self.break_at,br)
                self.break_at=br
                return

    def report_status(self,status='running',retcode=None,dump_status=True):
        """Report status to manager and dump status.json, if required"""
        msg={'status':status,
             'current_stage':self.stage,
             'break':self.break_at,
             'failures':self.failures}
        if retcode is not None:
            msg['retcode']=retcode
        self.manager_queue.put(msg)
        if dump_status:
            json.dump(msg,open(os.path.join(self.working_dir,'status.json')),indent=4)
 
    def failure(self,reason,dump_status=True):
        """Report a failure in the current stage"""
        self.failures.append({'stage':self.stage,'reason':reason})
        self.report_status(dump_status=dump_status)

    def set_stage(self,stage,status='running',dump_status=True):
        """Unconditionally switch stage and report status to manager"""
        self.stage=stage
        self.report_status(status,dump_status=dump_status)
       
    def next_stage(self,stage):
        """Switch to next test stage if allowed"""
        while not common.is_A_earlier_than_B(stage,self.break_at):
            #We have reached the set break
            #Waiting until another, later, break is set by manager
            self.get_next_break()
        self.set_stage(stage)
        
    def perform_test(self):
        """Perform the test sequence via TankCore"""
        retcode = 1

        self.set_stage('lock',dump_status=False)

        try:
            self.core.get_lock(force=False)
        except Exception:
            self.log.exception("Failed to obtain lock")
            self.failure('Failed to obtain lock',dump_status=False)
            self.report_status(status='failed',retcode=retcode,dump_status=False)
            return

        try:
            self.__preconfigure()

            self.next_stage('configure')
            self.core.plugins_configure()

            self.next_stage('prepare')
            self.core.plugins_prepare_test()

            self.next_stage('start')
            self.core.plugins_start_test()

            self.next_stage('poll')
            retcode = self.core.wait_for_finish()

        except KeyboardInterrupt:
            self.failure("Interrupted")
            self.log.info("Interrupted, trying to exit gracefully...")

        except Exception as ex:
            self.log.exception("Exception occured:")
            self.log.info("Trying to exit gracefully...")
            self.failure("Exception:" + traceback.format_exc(ex) )

        finally:
            try:
                self.next_stage('end')
                retcode = self.core.plugins_end_test(retcode)

                #We do NOT call post_process if end_test failed
                #Not sure if it is the desired behaviour
                self.next_stage('postprocess')
                retcode = self.core.plugins_post_process(retcode)
            except KeyboardInterrupt:
                self.failure("Interrupted")
                self.log.info("Interrupted during test shutdown...")
            except Exception as ex:
                self.log.exception("Exception occured while finishing test")
                self.failure("Exception:" + traceback.format_exc(ex) )
            finally:            
                self.next_stage('unlock')
                self.core.release_lock()
                self.set_stage('finish')
                self.report_status(status='failed' if self.failures else 'success',
                                   retcode=retcode)
        self.log.info("Done performing test with code %s", retcode)


def run(tank_queue,manager_queue,work_dir):
    """
    Target for tank process.
    This is the only function from this module ever used by Manager.

    tank_queue
        Read next break from here

    manager_queue
        Write tank status there
       
    """
    TankWorker(tank_queue,manager_queue,work_dir).perform_test()
    